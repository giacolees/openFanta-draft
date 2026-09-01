"""Test del contratto di valutazione WP7 — tre valori disgiunti (WP7).

Coprono ``valuation_contract(auction, p, team) -> {"market", "fantasy",
"my_team"}`` (API pura senza ricorsione su ``Auction.evaluate``) e la sua
propagazione in ``evaluate``, ``player_payload``/``/api/eval`` e
``format_eval``:

- **separazione forte**: modificare il budget (top-up) cambia ``maxbid`` ma
  NON market/fantasy/team_value; modificare le statistiche fantacalcistiche
  cambia fantasy/team_value ma NON market; cambiare rosa/team può cambiare
  my_team ma NON market/fantasy;
- **market**: ``expected`` == ``suggested`` legacy invariato; range che
  contiene sempre ``expected`` ed è >= 1, con le tre fonti documentate
  (range PFC scalato, fallback ``unc_pfc``, fallback conservativo ±20%);
- **fantasy**: blocco non monetario (utility/score, indici, status/risk,
  slot/fascia) — nessun prezzo dentro il blocco;
- **my_team**: ``team_value`` = expected × value_ratio × marginal_gain ×
  need, intero e bounded, indipendente da affordabilità/budget corrente;
  covered => 0 con reason, untracked => None;
- **contratto API**: chiavi esatte dei tre blocchi in evaluate e payload web;
  snapshot ``suggested``/``maxbid`` legacy invariati;
- **robustezza**: nessun NaN / bounds violati su tutto il listone reale e su
  stati dopo sequenze di vendite; smoke del testo CLI a tre sezioni.
"""

import math

import pytest  # pyright: ignore[reportMissingImports]
import valuation as v  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import DEFAULT_FORMATION, DEFAULT_SLOTS, make_player
from live_auction import (  # pyright: ignore[reportMissingImports]
    Auction,
    load_players,
)

MARKET_KEYS = {
    "expected",
    "range",
    "range_source",
    "pfc",
    "pfc_range",
    "pfc_lo",
    "pfc_hi",
    "pma",
    "pma_range",
    "pma_lo",
    "pma_hi",
    "unc_pfc",
    "dpfcpma",
    "infl",
    "scarc",
    "disc",
    "unsold",
}
FANTASY_KEYS = {
    "utility",
    "score",
    "expfm",
    "tit",
    "tix",
    "fix",
    "fix_contrib",
    "pen_prob",
    "fk_prob",
    "status",
    "risk",
    "slot",
    "fascia",
}
MY_TEAM_KEYS = {
    "role",
    "team",
    "tracked",
    "covered",
    "expected",
    "formula",
    "value_ratio",
    "marginal_gain",
    "need",
    "gain_need",
    "holes_ratio",
    "n_alt",
    "alternative",
    "team_value",
    "reason",
}


def mk(nome, **kw):
    """make_player di conftest + attributi WP6 (expfm/tix/fix)."""
    base_kw = {
        k: kw[k]
        for k in ("ruolo", "pfc", "slot", "tit", "status", "fix_contrib", "squadra")
        if k in kw
    }
    p = make_player(nome, **base_kw)
    for k in ("expfm", "tix", "fix"):
        if k in kw:
            p[k] = kw[k]
    return p


def make_a_pool(n, **kw):
    return [mk(f"A{i:02d}", **kw) for i in range(1, n + 1)]


def make_auction(pool, **over):
    """Auction di test con formation sempre <= slots (invariante strutturale)."""
    kwargs = {"teams": 3, "budget": 100, "slots": dict(DEFAULT_SLOTS)}
    kwargs.update(over)
    slots = kwargs["slots"]
    kwargs["formation"] = {
        r: min(DEFAULT_FORMATION[r], slots[r]) for r in DEFAULT_FORMATION
    }
    return Auction([dict(q) for q in pool], **kwargs)


def top_up(auction, team, amount):
    """Top-up del budget di ``team``: denaro della lega e totale iniziale
    crescono insieme → inflazione invariata e invarianti del ledger rispettate
    (check_invariants resta []). È l'unica mutazione che cambia il budget di
    una squadra senza toccare prezzi né mercato."""
    auction.state["money"][team] += amount
    auction.state["money_league"] += amount
    auction.total_money0 += amount


def assert_contract_bounded(a, p, team, e=None):
    """Bounds del contratto: tutti i numeri finiti, range >= 1 che contiene
    expected, team_value intero >= 0 (o None)."""
    mkt = e["market"] if e else v.market_block(a, p)
    fan = e["fantasy"] if e else v.fantasy_block(p)
    mt = e["my_team"] if e else v.team_value_breakdown(a, p, team)
    for key in ("expected", "infl", "scarc", "disc", "pfc"):
        val = mkt[key]
        assert math.isfinite(val), f"market.{key} non finito"
    assert mkt["expected"] >= 1
    assert mkt["range"]["lo"] >= 1
    assert mkt["range"]["lo"] <= mkt["expected"] <= mkt["range"]["hi"]
    assert mkt["range_source"] in {
        "scaled_pfc_range",
        "unc_pfc",
        "conservative_fallback",
    }
    assert 0.0 <= fan["utility"] <= 1.0
    assert 0 <= fan["score"] <= 100
    assert fan["risk"] in (1.0, 0.75, 0.5, 0.6)
    if mt["team_value"] is None:
        assert mt["reason"] == "untracked"
    else:
        assert isinstance(mt["team_value"], int) and mt["team_value"] >= 0
        assert mt["team_value"] <= 3 * mkt["expected"]  # bounded dalla formula


# ------------------------------------------------- contratto: chiavi e API pura
def test_contract_chiavi_esatte_dei_tre_blocchi(build, by_name):
    a = build()
    e = a.evaluate(by_name(a, "ALPHA"), "IO")
    assert set(e["market"]) == MARKET_KEYS
    assert set(e["fantasy"]) == FANTASY_KEYS
    assert set(e["my_team"]) == MY_TEAM_KEYS
    # i tre blocchi sono additivi su evaluate, nessuna chiave legacy rimossa
    for key in (
        "base",
        "infl",
        "scarc",
        "disc",
        "suggested",
        "maxbid",
        "covered",
        "tracked",
        "alt",
        "alt_value",
        "demand",
        "money",
        "slots_role",
        "slots_left",
        "scarcity_team",
        "scarcity_breakdown",
        "maxbid_breakdown",
    ):
        assert key in e, key


def test_valuation_contract_api_pura_coerente_con_evaluate(build, by_name):
    """valuation_contract è chiamabile da sola (nessuna ricorsione su
    evaluate) e i tre blocchi coincidono con quelli esposti da evaluate."""
    a = build()
    p = by_name(a, "ALPHA")
    contract = v.valuation_contract(a, p, "IO")
    assert set(contract) == {"market", "fantasy", "my_team"}
    e = a.evaluate(p, "IO")
    for block in ("market", "fantasy", "my_team"):
        assert contract[block] == e[block], block
    # default team = cfg["io"]
    assert v.valuation_contract(a, p) == v.valuation_contract(a, p, "IO")


def test_fantasy_blocco_senza_prezzi(build, by_name):
    """Per contratto il blocco fantasy non contiene alcun prezzo/budget."""
    a = build()
    fan = a.evaluate(by_name(a, "ALPHA"), "IO")["fantasy"]
    assert set(fan) == FANTASY_KEYS
    for banned in ("base", "pfc", "expected", "maxbid", "prezzo", "pma", "money"):
        assert banned not in fan, banned
    # i numeri non monetari ci sono tutti e coerenti con la statistica
    p = by_name(a, "ALPHA")
    assert fan["utility"] == pytest.approx(v.fantasy_utility(p))
    assert fan["score"] == round(v.fantasy_utility(p) * 100)
    assert fan["status"] == "T" and fan["risk"] == 1.0
    assert fan["slot"] == p["slot"] and fan["fascia"] == p["fascia"]


# ------------------------------------------------------------- blocco market
def test_market_expected_uguale_suggested_legacy(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    e = a.evaluate(p, "IO")
    disc = a.cfg["unsold_discount"] ** a.state["unsold"].get(p["pid"], 0)
    legacy = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
    assert e["market"]["expected"] == e["suggested"] == legacy
    assert e["market"]["expected"] == v.live_price(a, p)
    # identico anche dopo vendite ALTRO + invenduti (sconto)
    a.mark_sold(by_name(a, "BETA"), 40, None)
    a.mark_unsold(by_name(a, "ALPHA"))
    e2 = a.evaluate(p, "IO")
    disc2 = a.cfg["unsold_discount"] ** a.state["unsold"].get(p["pid"], 0)
    legacy2 = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc2))
    assert e2["market"]["expected"] == e2["suggested"] == legacy2
    assert e2["market"]["disc"] == pytest.approx(disc2)


def test_market_range_scala_pfc_range(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    p["pfc_lo"], p["pfc_hi"] = 45.0, 55.0
    e = a.evaluate(p, "IO")
    mkt = e["market"]
    f = mkt["infl"] * mkt["scarc"] * mkt["disc"]
    assert mkt["range"] == {"lo": round(45.0 * f), "hi": round(55.0 * f)}
    assert mkt["range_source"] == "scaled_pfc_range"
    assert mkt["range"]["lo"] <= mkt["expected"] <= mkt["range"]["hi"]
    # dopo vendite il range scala con gli stessi fattori live del prezzo atteso
    a.mark_sold(by_name(a, "BETA"), 40, "T1")
    e2 = a.evaluate(p, "IO")
    f2 = e2["market"]["infl"] * e2["market"]["scarc"] * e2["market"]["disc"]
    assert e2["market"]["range"] == {"lo": round(45.0 * f2), "hi": round(55.0 * f2)}
    assert e2["market"]["range_source"] == "scaled_pfc_range"


def test_market_range_fallback_unc_pfc():
    p = make_a_pool(1, pfc=50.0)[0]  # senza pfc_lo/hi, senza unc_pfc
    p["unc_pfc"] = 5.0
    a = make_auction([p], teams=1)
    mkt = v.market_block(a, a.find("A01"))
    assert mkt["expected"] == 50
    assert mkt["range"] == {"lo": 45, "hi": 55}
    assert mkt["range_source"] == "unc_pfc"
    assert mkt["range"]["lo"] <= mkt["expected"] <= mkt["range"]["hi"]


def test_market_range_fallback_conservativo(build, by_name):
    a = build()
    mkt = a.evaluate(by_name(a, "ALPHA"), "IO")["market"]  # nessun range/unc
    assert mkt["range_source"] == "conservative_fallback"
    assert mkt["range"]["lo"] == round(mkt["expected"] * 0.8)
    assert mkt["range"]["hi"] == round(mkt["expected"] * 1.2)
    assert mkt["range"]["lo"] >= 1
    assert mkt["range"]["lo"] <= mkt["expected"] <= mkt["range"]["hi"]


def test_market_range_contiene_sempre_expected(build, by_name):
    """Anche con arrotondamenti estremi il range contiene expected e lo è >= 1."""
    a = build()
    p = by_name(a, "ALPHA")
    p["pfc_lo"], p["pfc_hi"] = 45.6, 45.7  # lo arrotonda sopra expected (45.5->44)
    e = a.evaluate(p, "IO")
    rng = e["market"]["range"]
    assert rng["lo"] >= 1
    assert rng["lo"] <= e["market"]["expected"] <= rng["hi"]


def test_market_solo_input_di_prezzo_non_per_singola_squadra(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    e_io = a.evaluate(p, "IO")["market"]
    e_t1 = a.evaluate(p, "T1")["market"]
    assert e_io == e_t1  # il mercato non dipende da chi guarda
    # nessuna chiave squadra-dipendente nel blocco
    for banned in ("scarcity_team", "team_component", "covered", "tracked"):
        assert banned not in e_io, banned


# ------------------------------------------------------------------ my_team
def test_team_value_formula_breakdown(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    mt = v.team_value_breakdown(a, p, "IO")
    assert mt["formula"] == "expected × value_ratio × marginal_gain × need"
    assert mt["expected"] == v.live_price(a, p)
    util = v.fantasy_utility(p)
    alt = v.best_alternative(a, p, "IO")
    assert mt["value_ratio"] == pytest.approx(util / v.fantasy_utility(alt))
    assert mt["marginal_gain"] == pytest.approx(v.roster_marginal_gain(a, p, "IO"))
    # need = clamp(1 + 0.5·holes_ratio − 0.1·n_alt): IO non ha comprato nulla
    # nel ruolo A (holes_ratio 1.0), 3 alternative residue -> 1 + 0.5 − 0.3
    assert mt["n_alt"] == 3
    assert mt["holes_ratio"] == 1.0
    assert mt["need"] == pytest.approx(1.2)
    assert mt["gain_need"] == pytest.approx(mt["marginal_gain"] * mt["need"])
    assert mt["team_value"] == round(
        mt["expected"] * mt["value_ratio"] * mt["gain_need"]
    )
    # formula del valore: team_value calcolato dal breakdown
    assert mt["team_value"] == round(
        mt["expected"] * mt["value_ratio"] * mt["marginal_gain"] * mt["need"]
    )


def test_team_value_nessuna_alternativa_ratio_neutro():
    pool = [mk("SOLO")]
    a = make_auction(pool, teams=1)
    mt = v.team_value_breakdown(a, a.find("SOLO"), "IO")
    assert mt["alternative"] is None
    assert mt["value_ratio"] == 1.0
    assert mt["team_value"] is not None and mt["team_value"] >= 1


def test_team_value_mai_negativo_e_bounded(build, by_name):
    for over in (
        {},
        {"budget": 10},
        {"budget": 50},
        {"budget": 200},
        {"role_budget_weights": {"P": 0.3, "D": 0.3, "C": 0.3, "A": 0.1}},
        {"role_floor_price": {"P": 3, "D": 2, "C": 2, "A": 4}},
    ):
        a = build(**over)
        p = by_name(a, "ALPHA")
        e = a.evaluate(p, "IO")
        assert_contract_bounded(a, p, "IO", e)


def test_team_value_coperto_zero_con_reason(build, by_name):
    pool = [mk(f"A{i:02d}") for i in range(1, 7)]
    a = build(players=pool, teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "A01"), 10, "IO")  # IO copre l'unico slot A
    mt = v.team_value_breakdown(a, by_name(a, "A02"), "IO")
    assert mt["covered"] and mt["team_value"] == 0 and mt["reason"] == "covered"
    e = a.evaluate(a.find("A02"), "IO")
    assert e["my_team"] == mt
    assert e["covered"]
    # il blocco market/fantasy resta valido anche coperto
    assert_contract_bounded(a, by_name(a, "A02"), "IO", e)


def test_team_value_untracked_none(build, by_name):
    a = build()
    for team in ("ALTRO", "ROMA"):
        mt = v.team_value_breakdown(a, by_name(a, "ALPHA"), team)
        assert not mt["tracked"]
        assert mt["team_value"] is None and mt["reason"] == "untracked"
        e = a.evaluate(by_name(a, "ALPHA"), team)
        assert e["my_team"]["team_value"] is None
        assert_contract_bounded(a, by_name(a, "ALPHA"), team, e)


# ------------------------------------------------- separazione forte (WP7)
def test_separazione_budget_cambia_maxbid_non_market_fantasy_team():
    """Top-up del budget di IO: cambia maxbid (cap riserva), mai market,
    fantasy o team_value (il valore intrinseco non dipende da chi può pagare)."""
    a = make_auction(make_a_pool(6), teams=1, budget=50)
    p = a.find("A01")
    e0 = a.evaluate(p, "IO")
    mb0 = v.max_bid_breakdown(a, p, "IO")
    assert mb0["binding_cap"] == "reserve_cap"  # 50 − 28 = 22 vincola
    assert e0["maxbid"] == 22

    top_up(a, "IO", 10)
    assert a.check_invariants() == []
    e1 = a.evaluate(p, "IO")
    mb1 = v.max_bid_breakdown(a, p, "IO")
    assert mb1["reserve_cap"] == 32 and e1["maxbid"] == 32  # maxbid cambiato

    # market / fantasy / my_team identici (nessuna dipendenza dal budget)
    for block in ("market", "fantasy", "my_team"):
        assert e0[block] == e1[block], block
    # in particolare il valore per la rosa non si muove
    assert e1["my_team"]["team_value"] == e0["my_team"]["team_value"]


def test_separazione_statistiche_fantasy_cambiano_fantasy_team_non_market():
    """Mutare la statistica fantacalcistica del candidato (tit) cambia
    fantasy e team_value, MAI market (base/inflazione/scarsità/sconto non
    dipendono da tit/expfm)."""
    a = make_auction(make_a_pool(4))
    p = a.find("A01")
    e0 = a.evaluate(p, "IO")
    f0, m0, t0 = e0["fantasy"], e0["market"], e0["my_team"]
    a.players[p["pid"]]["tit"] = 95.0  # il motore possiede i dict (deep-copy)
    e1 = a.evaluate(p, "IO")
    f1, m1, t1 = e1["fantasy"], e1["market"], e1["my_team"]
    assert f1["utility"] > f0["utility"] and f1["score"] > f0["score"]
    assert m1 == m0  # prezzo di mercato identico
    assert t1["value_ratio"] > t0["value_ratio"]
    assert t1["marginal_gain"] > t0["marginal_gain"]
    assert t1["team_value"] != t0["team_value"]


def test_separazione_rosa_cambia_my_team_non_market_fantasy():
    """Due squadre con rose diverse nella stessa asta: my_team può cambiare,
    market/fantasy restano identici (sono indipendenti dalla squadra)."""
    pool = [mk(f"S{i:02d}", tit=95.0, expfm=7.5) for i in range(1, 5)] + [
        mk(f"W{i:02d}") for i in range(1, 5)
    ]
    a = make_auction(pool, teams=2)
    for i in range(1, 5):
        a.mark_sold(a.find(f"S{i:02d}"), 10, "T1")  # T1: formazione A completa
    w = a.find("W01")
    e_io = a.evaluate(w, "IO")
    e_t1 = a.evaluate(w, "T1")
    assert e_io["market"] == e_t1["market"]  # mercato identico
    assert e_io["fantasy"] == e_t1["fantasy"]  # fantasy identico
    assert e_io["my_team"] != e_t1["my_team"]  # rosa diversa -> my_team diverso
    assert e_t1["my_team"]["marginal_gain"] == 0.0  # rosa già migliore
    assert e_t1["my_team"]["team_value"] == 0
    assert e_io["my_team"]["team_value"] > 0
    # il budget di T1 (ridotto dagli acquisti) NON entra in team_value
    assert e_t1["my_team"]["team_value"] == 0  # zero per gain, non per budget


# ------------------------------------------------------- contratto API web
def test_payload_api_propaga_contratto():
    pool = make_a_pool(6)
    wa.PLAYERS = pool
    wa.engine = wa.TrendAuction(
        [dict(q) for q in pool],
        teams=3,
        budget=100,
        slots=dict(DEFAULT_SLOTS),
        formation={
            r: min(DEFAULT_FORMATION[r], DEFAULT_SLOTS[r]) for r in DEFAULT_FORMATION
        },
    )
    p = wa.engine.find("A01")
    payload = wa.player_payload(wa.engine, p, "IO")
    assert set(payload["market"]) == MARKET_KEYS
    assert set(payload["fantasy"]) == FANTASY_KEYS
    assert set(payload["my_team"]) == MY_TEAM_KEYS
    assert payload["market"]["expected"] == payload["suggested"]
    assert payload["maxbid"] == payload["maxbid_breakdown"]["final"]
    assert (
        payload["my_team"]["team_value"]
        == v.team_value_breakdown(wa.engine, p, "IO")["team_value"]
    )
    # anche con team=None (default io) e via /api/eval
    payload2 = wa.player_payload(wa.engine, p, None)
    assert payload2["market"] == payload["market"]
    resp = wa.api_eval(key="A01", team="IO")
    assert isinstance(resp, dict)
    assert resp["market"] == payload["market"]
    assert resp["my_team"]["team_value"] == payload["my_team"]["team_value"]


def test_payload_api_giocatore_venduto_contratto_none():
    pool = make_a_pool(4)
    wa.PLAYERS = pool
    wa.engine = wa.TrendAuction(
        [dict(q) for q in pool],
        teams=2,
        budget=100,
        slots=dict(DEFAULT_SLOTS),
        formation={
            r: min(DEFAULT_FORMATION[r], DEFAULT_SLOTS[r]) for r in DEFAULT_FORMATION
        },
    )
    wa.engine.mark_sold(wa.engine.find("A01"), 50, "IO")
    resp = wa.api_eval(key="A01", team="IO")  # giocatore venduto: errore
    assert "error" in resp and resp["suggested"] is None
    assert resp.get("market") is None  # contratto assente sul percorso errore


# --------------------------------------------------- snapshot legacy invariati
def test_snapshot_suggested_maxbid_legacy_invariati(build, by_name):
    """I numeri legacy (suggested, maxbid) sono identici al pre-WP7: il
    contratto è ADDITIVO, non ricalibra nulla."""
    a = build()
    disc = 1.0
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        p = by_name(a, nome)
        e = a.evaluate(p, "IO")
        legacy = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
        assert e["suggested"] == legacy == e["market"]["expected"]
        mb = e["maxbid_breakdown"]
        assert e["maxbid"] == mb["final"] == mb["maxbid"]
    # dopo vendite miste (ALTRO + invenduti) la formula legacy resta identica
    a.mark_sold(by_name(a, "BETA"), 40, None)
    a.mark_unsold(by_name(a, "ALPHA"))
    a.mark_unsold(by_name(a, "ALPHA"))
    for nome in ("ALPHA", "GAMMA", "DELTA"):
        p = by_name(a, nome)
        d = a.cfg["unsold_discount"] ** a.state["unsold"].get(p["pid"], 0)
        e = a.evaluate(p, "IO")
        legacy = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * d))
        assert e["suggested"] == legacy == e["market"]["expected"]


# ------------------------------------------------------------- testo CLI
def test_format_eval_tre_sezioni(build, by_name):
    a = build()
    out = a.format_eval(by_name(a, "ALPHA"), "IO")
    # tre sezioni con etichette non ambigue
    for sec in (
        "── Mercato previsto ──",
        "── Valore fantacalcistico ──",
        "── Valore per la mia rosa ──",
    ):
        assert sec in out, sec
    assert "Prezzo di mercato atteso:" in out
    assert "range" in out and "fonte:" in out
    assert "Utility:" in out and "score" in out
    assert "Valore per la rosa:" in out
    assert "mercato atteso" in out and "× ratio" in out
    # legacy WP6 ancora visibili
    assert "Max offerta IO:" in out
    for tok in ("cap mercato", "cap riserva", "cap ruolo", "cap opportunità"):
        assert tok in out, tok


def test_format_eval_covered_untracked_non_crash(build, by_name):
    a = build()
    # untracked: nessun valore per la rosa, nessun crash
    out = a.format_eval(by_name(a, "ALPHA"), "ALTRO")
    assert "Valore per la rosa: non calcolabile" in out
    assert "squadra non tracciata" in out
    # covered: valore 0 con motivo esplicito
    pool = [mk(f"A{i:02d}") for i in range(1, 7)]
    a2 = build(players=pool, teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a2.mark_sold(by_name(a2, "A01"), 10, "IO")
    out2 = a2.format_eval(by_name(a2, "A02"), "IO")
    assert "Valore per la rosa: 0 cr" in out2 and "coperto" in out2
    assert "esauriti" in out2


# ------------------------------------------------- robustezza sul listone reale
REAL_CSV = None  # risolto lazy nel test (path relativo al repo)


def _real_auction(teams=8, budget=500):
    from pathlib import Path

    csv_path = Path(__file__).resolve().parent.parent / "data" / "listone.csv"
    players = load_players(str(csv_path))
    return players, Auction(players, teams=teams, budget=budget)


def test_bounds_su_tutto_il_listone_reale():
    players, a = _real_auction()
    assert len(players) >= 500  # listone reale non banale
    teams = list(a.state["money"])
    # scan completo per la squadra di default + campione deterministico per le
    # altre (tempo: O(P^2), il campione tiene il test leggero)
    sample = sorted(a.state["pool"])[:: max(1, len(a.state["pool"]) // 60)]
    for team in teams:
        scan = a.state["pool"] if team == a.cfg["io"] else sample
        for pid in scan:
            p = a.players[pid]
            e = a.evaluate(p, team)
            assert_contract_bounded(a, p, team, e)
            # il contratto puro coincide con quello esposto da evaluate
            assert e["my_team"] == v.valuation_contract(a, p, team)["my_team"]


def test_no_nan_stati_dopo_vendite_listone_reale():
    _players, a = _real_auction(teams=8, budget=500)
    # sequenza di vendite mista (tracciate, ALTRO, invenduti)
    for nome, price, team in (
        ("MCTOMINAY", 140, "IO"),
        ("DIMARCO", 120, "T1"),
        ("PAZ N.", 90, None),
        ("MALEN", 300, "T2"),
        ("BARELLA", 60, "IO"),
    ):
        p = a.find(nome)
        if p is not None and p["pid"] in a.state["pool"]:
            a.mark_sold(p, min(price, a.state["money_league"]), team)
    for nome in ("ZIELINSKI", "CALHANOGLU", "PULISIC"):
        p = a.find(nome)
        if p is not None:
            a.mark_unsold(p)
    assert a.check_invariants() == []
    # ogni giocatore ancora nel pool, per ogni squadra tracciata e ALTRO
    for pid in list(a.state["pool"])[:400]:
        p = a.players[pid]
        for team in list(a.state["money"]) + ["ALTRO"]:
            e = a.evaluate(p, team)
            assert_contract_bounded(a, p, team, e)
    # anche lo stato iniziale resta sano dopo undo totale
    while a.undo():
        pass
    assert a.check_invariants() == []
