"""Test del maxbid a 4 cap trasparenti (openfanta.core.valuation) — WP6.

Coprono il modello che sostituisce il maxbid legacy ``suggested × aggression``:

- ``fantasy_utility``: bounded [0,1], monotona rispetto a tit/expfm/status,
  rischio status (T > B > P) e status sconosciuto;
- ``roster_marginal_gain``: buchi titolari → gain pieno; rosa già titolare
  nel ruolo → gain scontato (rosa forte riduce il gain); squadra non
  tracciata → 0;
- ``best_alternative``: migliore alternativa residua schierabile (stesso
  ruolo, escluso il candidato), deterministica, None se ruolo coperto;
- ``reserve_after_purchase``: slot residui dopo il candidato × floor per
  ruolo (default piatti = comportamento invariato);
- ``role_budget_left``: allocazione per ruolo — peso da config
  (``role_budget_weights``) o dalla composizione PFC iniziale; target/speso/
  rilascio dei ruoli completati; None per squadre non tracciate;
- ``opportunity_cap``: prezzo live dell'alternativa × rapporto di utility
  (clamp 0.5–2.0) × urgenza (buchi titolari ↑, alternative abbondanti ↓,
  scarsità team ↑) — nessuna alternativa → cap None;
- ``max_bid_breakdown`` / ``evaluate``: 4 cap separati, ``final = min(cap
  validi)`` intero ≥ 0, ``binding_cap`` esplicito, top-level ``maxbid`` ==
  final; ruolo coperto / squadra non tracciata → None; final NON forzato
  sopra ``base``; ``suggested`` identico alla formula pre-WP6 in ogni stato;
- monoticità budget/riserva, bounds e invarianti dopo sequenze di vendite;
- payload API web che propaga ``maxbid_breakdown``.
"""

import pytest  # pyright: ignore[reportMissingImports]
from conftest import DEFAULT_FORMATION, DEFAULT_SLOTS, make_player

import openfanta.web.app as wa  # pyright: ignore[reportMissingImports]
from openfanta.core import valuation as v  # pyright: ignore[reportMissingImports]

CAP_KEYS = ("market_cap", "reserve_cap", "role_cap", "opportunity_cap")


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


def make_a_pool(n):
    return [mk(f"A{i:02d}") for i in range(1, n + 1)]


def assert_breakdown_consistente(mb):
    """Invarianti del breakdown: final == min(cap validi) >= 0, mai sotto i
    cap; maxbid == final; binding_cap == chiave del minimo."""
    assert mb["maxbid"] == mb["final"]
    if mb["final"] is None:
        assert all(mb[k] is None for k in CAP_KEYS)
        assert mb["binding_cap"] is None
        return
    caps = [mb[k] for k in CAP_KEYS]
    assert all(c is not None and isinstance(c, int) and c >= 0 for c in caps)
    assert mb["final"] == min(caps)
    assert mb["binding_cap"] in CAP_KEYS and mb[mb["binding_cap"]] == mb["final"]
    assert mb["opportunity_cap"] is None or mb["opportunity_cap"] >= 1


# ------------------------------------------------------------- fantasy utility
def test_utility_bounded_e_monotona(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    u = v.fantasy_utility(p)
    assert 0.0 <= u <= 1.0
    assert u == pytest.approx(
        0.4625
    )  # default conftest: (0.35*.6 + .25*.25 + .15*.5 + .15*.5 + .1*.4)
    # tit/expfm più alti -> utility più alta (stesso ruolo/base)
    best = mk("TOP", tit=95.0, expfm=7.5, fix_contrib=1.2, tix=90.0, fix=90.0)
    assert v.fantasy_utility(best) > u
    # valori estremi restano bounded
    extreme = dict(p)
    extreme["tit"] = 10_000.0
    extreme["expfm"] = 99.0
    assert v.fantasy_utility(extreme) <= 1.0
    assert v.fantasy_utility(mk("ZERO", tit=-5.0, expfm=0.0, fix_contrib=-9.0)) >= 0.0


def test_utility_rischio_status(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    u_t = v.fantasy_utility(p)
    pb = dict(p)
    pb["status"] = "B"
    pp = dict(p)
    pp["status"] = "P"
    px = dict(p)
    px["status"] = "?"
    assert v.fantasy_utility(pb) == pytest.approx(u_t * 0.75)
    assert v.fantasy_utility(pp) == pytest.approx(u_t * 0.5)
    assert v.fantasy_utility(px) == pytest.approx(u_t * 0.6)
    assert u_t > v.fantasy_utility(pb) > v.fantasy_utility(pp)


# ------------------------------------------------------------ marginal gain
def test_marginal_gain_buchi_titolari_gain_pieno(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    # IO non ha ancora comprato nulla nel ruolo A (formation A = 4, filled 0)
    assert v.roster_marginal_gain(a, p, "IO") == pytest.approx(v.fantasy_utility(p))


def test_marginal_gain_rosa_forte_riduce(build, by_name):
    # 4 top nel ruolo A (formation A = 4) già acquistati: il candidato base è
    # rotazione rispetto a titolari migliori -> gain 0 (peggior titolare > candidato)
    pool = [mk(f"S{i:02d}", tit=95.0, expfm=7.5) for i in range(1, 5)] + [
        mk(f"W{i:02d}") for i in range(1, 5)
    ]
    fresh = build(players=pool)
    full = build(players=pool)
    for i in range(1, 5):
        full.mark_sold(full.find(f"S{i:02d}"), 10, "IO")
    w = fresh.find("W01")
    gain_fresh = v.roster_marginal_gain(fresh, w, "IO")
    gain_full = v.roster_marginal_gain(full, w, "IO")
    assert gain_fresh == pytest.approx(v.fantasy_utility(w))
    assert gain_full < gain_fresh
    assert gain_full == 0.0  # peggior titolare (0.875) > candidato (0.4625)


def test_marginal_gain_untracked_zero(build, by_name):
    a = build()
    assert v.roster_marginal_gain(a, by_name(a, "ALPHA"), "ALTRO") == 0.0


# --------------------------------------------------------- best_alternative
def test_best_alternative_stesso_ruolo_escluso_candidato(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    alt = v.best_alternative(a, p, "IO")
    assert alt is not None
    assert alt["ruolo"] == "A"
    assert alt["pid"] != p["pid"]
    assert alt["pid"] in a.state["pool"]
    # determinismo: stessa domanda -> stesso risultato su copie identiche
    assert alt["pid"] == v.best_alternative(a, p, "IO")["pid"]


def test_best_alternative_candidato_migliore_della_rosa(build, by_name):
    pool = [mk("TOP", tit=95.0, expfm=7.5)] + [mk(f"W{i:02d}") for i in range(1, 4)]
    a = build(players=pool)
    # la migliore alternativa di TOP è la migliore residua (W01/02/03: tutte uguali)
    alt = v.best_alternative(a, a.find("TOP"), "IO")
    assert alt["nome"].startswith("W")


def test_best_alternative_coperto_none(build, by_name):
    pool = [mk(f"A{i:02d}") for i in range(1, 7)]
    a = build(players=pool, teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "A01"), 10, "IO")  # IO copre l'unico slot A
    assert v.best_alternative(a, by_name(a, "A02"), "IO") is None


def test_best_alternative_untracked_none(build, by_name):
    a = build()
    assert v.best_alternative(a, by_name(a, "ALPHA"), "ALTRO") is None


# ------------------------------------------------------ reserve_after_purchase
def test_reserve_after_purchase_default(build, by_name):
    a = build()
    r = v.reserve_after_purchase(a, by_name(a, "ALPHA"), "IO")
    # slot residui dopo il candidato A: P3 D3 C3 A(6-1) = 14 x floor 2
    assert r["per_role"] == {"P": 6, "D": 6, "C": 6, "A": 10}
    assert r["total"] == 28


def test_reserve_after_purchase_floor_per_ruolo(build, by_name):
    a = build(role_floor_price={"P": 3, "D": 2, "C": 2, "A": 4})
    r = v.reserve_after_purchase(a, by_name(a, "ALPHA"), "IO")
    assert r["total"] == 3 * 3 + 3 * 2 + 3 * 2 + 5 * 4  # 41
    assert r["per_role"] == {"P": 9, "D": 6, "C": 6, "A": 20}


def test_reserve_after_purchase_untracked_zero(build, by_name):
    a = build()
    assert v.reserve_after_purchase(a, by_name(a, "ALPHA"), "ALTRO")["total"] == 0


def test_reserve_after_purchase_mai_superiore_a_riserva_iniziale(build, by_name):
    a = build()
    r0 = v.reserve_floor(a, "IO")
    r1 = v.reserve_after_purchase(a, by_name(a, "ALPHA"), "IO")
    assert r1["total"] < r0["total"]  # lo slot del candidato è già consumato


# ---------------------------------------------------------------- role budget
def test_role_weights_default_da_pfc_composition(build):
    a = build()
    w = v._role_weights(a)
    assert w == {"P": 0.0, "D": 0.0, "C": 0.0, "A": 1.0}  # solo A nel pool
    assert sum(w.values()) == pytest.approx(1.0)


def test_role_weights_override_da_config(build):
    a = build(role_budget_weights={"P": 2, "D": 1, "C": 1, "A": 4})
    w = v._role_weights(a)
    assert w["A"] == pytest.approx(0.5) and w["P"] == pytest.approx(0.25)
    assert sum(w.values()) == pytest.approx(1.0)


def test_role_budget_left_target_e_speso(build, by_name):
    a = build()
    r = v.role_budget_left(a, "IO", "A")
    assert r["target"] == 100 and r["spent"] == 0 and r["left"] == 100
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    r2 = v.role_budget_left(a, "IO", "A")
    assert r2["spent"] == 40 and r2["left"] == 60
    assert r2["source"] == "pfc_composition"


def test_role_budget_left_rilascio_ruoli_completati():
    pool = [mk(f"D{i:02d}", ruolo="D", pfc=30.0) for i in range(1, 4)] + [
        mk(f"A{i:02d}") for i in range(1, 7)
    ]
    a = wa_module_aux(
        pool,
        teams=1,
        budget=100,
        role_budget_weights={"P": 0.2, "D": 0.2, "C": 0.2, "A": 0.4},
    )
    # completa D (3 slot) spendendo 10: il budget D inutilizzato (10) viene
    # rilasciato ai ruoli aperti in proporzione al peso (A 0.4/0.8 = 0.5)
    a.mark_sold(a.find("D01"), 5, "IO")
    a.mark_sold(a.find("D02"), 3, "IO")
    a.mark_sold(a.find("D03"), 2, "IO")
    rA = v.role_budget_left(a, "IO", "A")
    assert rA["target"] == 40 and rA["spent"] == 0
    assert rA["released"] == 5 and rA["left"] == 45  # 40 + 5
    rD = v.role_budget_left(a, "IO", "D")
    assert rD["target"] == 20 and rD["spent"] == 10
    assert rD["released"] == 0 and rD["left"] == 10  # ruolo completato: nessun rilascio
    assert rA["source"] == "config"


def test_role_budget_left_untracked_none(build, by_name):
    a = build()
    assert v.role_budget_left(a, "ROMA", "A") is None


# ----------------------------------------------------------- opportunity cap
def test_opportunity_nessuna_alternativa_none(build, by_name):
    pool = [mk("SOLO")]
    a = build(players=pool, teams=1)
    opp = v.opportunity_cap(a, by_name(a, "SOLO"), "IO")
    assert opp["cap"] is None and opp["alternative"] is None
    assert opp["urgency"] is None


def test_opportunity_abbondante_stringe(build):
    a4 = build(players=make_a_pool(4))  # 4 A
    a8 = build(players=make_a_pool(8))  # 8 A (alternative abbondanti)
    p4 = a4.find("A01")
    p8 = a8.find("A01")
    o4 = v.opportunity_cap(a4, p4, "IO")
    o8 = v.opportunity_cap(a8, p8, "IO")
    assert o4["n_alt"] == 3 and o8["n_alt"] == 7
    assert o8["n_alt"] > o4["n_alt"]
    assert o8["urgency"] <= o4["urgency"]
    assert o8["cap"] < o4["cap"]
    assert o4["cap"] >= 1 and o8["cap"] >= 1


def test_opportunity_candidato_migliore_alza_solo_opportunity(build):
    pool = [mk("TOP", tit=95.0, expfm=7.5, fix_contrib=1.2, tix=90.0, fix=90.0)] + [
        mk(f"W{i:02d}") for i in range(1, 4)
    ]
    a = build(players=pool)
    mb_top = v.max_bid_breakdown(a, a.find("TOP"), "IO")
    mb_w = v.max_bid_breakdown(a, a.find("W01"), "IO")
    # stesso base => stesso prezzo di mercato e stessi cap mercato/riserva/ruolo
    assert mb_top["candidate"]["base"] == mb_w["candidate"]["base"]
    assert mb_top["suggested"] == mb_w["suggested"]
    for k in ("market_cap", "reserve_cap", "role_cap"):
        assert mb_top[k] == mb_w[k], k
    # il candidato chiaramente migliore alza SOLO il cap opportunità
    assert mb_top["opportunity_cap"] > mb_w["opportunity_cap"]
    assert mb_top["candidate"]["utility"] > mb_w["candidate"]["utility"]


def test_opportunity_rischio_riduce_cap(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    p_b = dict(p)
    p_b["status"] = "B"
    o_t = v.opportunity_cap(a, p, "IO")
    o_b = v.opportunity_cap(a, p_b, "IO")
    assert o_b["candidate_utility"] < o_t["candidate_utility"]
    assert o_b["cap"] <= o_t["cap"]  # ratio 0.75 -> cap più stretto
    assert v.fantasy_utility(p_b) == pytest.approx(v.fantasy_utility(p) * 0.75)


def test_opportunity_urgenza_rosa_forte():
    # stessa identica domanda per due squadre: solo la rosa (filled) cambia
    pool = [mk(f"S{i:02d}", tit=95.0, expfm=7.5) for i in range(1, 5)] + [
        mk(f"W{i:02d}") for i in range(1, 5)
    ]
    a = wa_module_aux(pool, teams=2)
    for i in range(1, 5):
        a.mark_sold(a.find(f"S{i:02d}"), 10, "T1")  # T1: formation A gia' coperta
    w = a.find("W01")
    o_io = v.opportunity_cap(a, w, "IO")  # IO: buchi titolari (holes 1)
    o_t1 = v.opportunity_cap(a, w, "T1")  # T1: rosa gia' titolare (holes 0)
    assert o_io["holes_ratio"] == 1.0 and o_t1["holes_ratio"] == 0.0
    assert o_t1["urgency"] <= o_io["urgency"]
    assert o_t1["cap"] <= o_io["cap"]


def test_opportunity_usa_prezzo_live_alternativa(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    opp = v.opportunity_cap(a, p, "IO")
    alt = a.players[opp["alternative"]["pid"]]
    assert opp["alternative"]["prezzo"] == v.live_price(a, alt)
    # l'alternativa usa la stessa formula del suggerito (mai il modello statistico)
    expected = max(1, round(alt["base"] * a.inflation() * a.scarcity(alt)))
    assert opp["alternative"]["prezzo"] == expected


# ------------------------------------------------------------ max_bid_breakdown
# ---- WP7: maxbid resta il limite sostenibile, separato dal valore per la rosa ----
def test_maxbid_separato_da_team_value_ma_coerente(build, by_name):
    a = build()
    e = a.evaluate(by_name(a, "ALPHA"), "IO")
    mt = e["my_team"]
    assert mt["team_value"] >= 0
    # maxbid (limite sostenibile, vincolato dal budget) e team_value (valore
    # intrinseco per la rosa) restano due numeri separati e non allineati
    assert e["maxbid"] == e["maxbid_breakdown"]["final"]
    assert mt["expected"] == e["suggested"]
    # il blocco market non contiene la scarsità per-singola squadra
    assert "scarcity_team" not in e["market"]
    # my_team espone la formula interpretabile
    assert "×" in mt["formula"] and "expected" in mt["formula"]


def test_breakdown_payload_chiavi(build, by_name):
    a = build()
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert set(mb) >= {
        "maxbid",
        "final",
        "binding_cap",
        "market_cap",
        "reserve_cap",
        "role_cap",
        "opportunity_cap",
        "reserve",
        "role",
        "alternative",
        "candidate",
        "tracked",
        "covered",
        "suggested",
    }
    assert mb["reserve"]["total"] == 28
    assert mb["reserve"]["per_role"] == {"P": 6, "D": 6, "C": 6, "A": 10}
    role = mb["role"]
    assert set(role) >= {"target", "spent", "released", "left", "weights"}
    cand = mb["candidate"]
    assert set(cand) >= {
        "utility",
        "risk",
        "status",
        "marginal_gain",
        "urgency",
        "base",
    }
    alt = mb["alternative"]
    assert alt["pid"] in a.state["pool"]
    assert set(alt) >= {"pid", "nome", "prezzo", "utility"}
    assert_breakdown_consistente(mb)


def test_breakdown_default_quattro_cap_binding(build, by_name):
    a = build()
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    # scenario WP5 con 4 alternative identiche: market x aggression e
    # opportunità coincidono sul tetto storico (62) -> invarianza numerica
    assert mb["market_cap"] == round(50 * a.cfg["aggression"])
    assert mb["reserve_cap"] == 72
    assert mb["role_cap"] == 100
    assert mb["opportunity_cap"] == 62
    assert mb["final"] == 62
    assert mb["binding_cap"] in ("market_cap", "opportunity_cap")
    assert mb["maxbid"] == 62


def test_cap_riserva_vincola(build, by_name):
    a = build(budget=50)
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert mb["reserve_cap"] == 50 - 28  # 22
    assert mb["binding_cap"] == "reserve_cap"
    assert mb["final"] == 22


def test_cap_ruolo_vincola(build, by_name):
    a = build(role_budget_weights={"P": 0.3, "D": 0.3, "C": 0.3, "A": 0.1})
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert mb["role_cap"] == 10  # 100 x 0.1
    assert mb["binding_cap"] == "role_cap"
    assert mb["final"] == 10


def test_cap_mercato_vincola(build, by_name):
    a = build(budget=200)
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert mb["reserve_cap"] == 172
    assert mb["role_cap"] == 200
    assert mb["binding_cap"] == "market_cap"
    assert mb["final"] == mb["market_cap"]


def test_cap_opportunita_vincola(build):
    a = build(players=make_a_pool(8))
    mb = v.max_bid_breakdown(a, a.find("A01"), "IO")
    assert mb["binding_cap"] == "opportunity_cap"
    assert mb["final"] == mb["opportunity_cap"]
    assert mb["final"] < mb["market_cap"]  # alternative abbondanti stringono


def test_final_mai_negativo_e_min_dei_cap(build, by_name):
    for over in (
        {},
        {"budget": 10},
        {"budget": 50},
        {"budget": 200},
        {"role_budget_weights": {"P": 0.3, "D": 0.3, "C": 0.3, "A": 0.1}},
        {"role_floor_price": {"P": 3, "D": 2, "C": 2, "A": 4}},
    ):
        a = build(**over)
        mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
        assert_breakdown_consistente(mb)


def test_final_non_forzato_sopra_base(build, by_name):
    a = build(budget=10)  # riserva 28 > crediti 10: cap riserva = 0
    mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert mb["final"] == 0
    assert mb["final"] < mb["candidate"]["base"]  # sotto il PFC, non forzato
    assert mb["binding_cap"] == "reserve_cap"


def test_breakdown_coperto_tutto_none(build, by_name):
    pool = [mk(f"A{i:02d}") for i in range(1, 7)]
    a = build(players=pool, teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "A01"), 10, "IO")
    e = a.evaluate(a.find("A02"), "IO")
    assert e["covered"] and e["maxbid"] is None
    mb = e["maxbid_breakdown"]
    assert mb["final"] is None and mb["maxbid"] is None
    assert all(mb[k] is None for k in CAP_KEYS)
    assert mb["binding_cap"] is None
    assert_breakdown_consistente(mb)


def test_breakdown_untracked_none(build, by_name):
    a = build()
    for team in ("ALTRO", "ROMA"):
        e = a.evaluate(by_name(a, "ALPHA"), team)
        assert not e["tracked"] and e["maxbid"] is None
        mb = e["maxbid_breakdown"]
        assert mb["final"] is None
        assert all(mb[k] is None for k in CAP_KEYS)


def test_maxbid_uguale_final_evaluate(build, by_name):
    a = build()
    e = a.evaluate(by_name(a, "ALPHA"), "IO")
    assert e["maxbid"] == e["maxbid_breakdown"]["final"]
    assert e["maxbid_breakdown"]["maxbid"] == e["maxbid_breakdown"]["final"]


# ----------------------------------------------------------------- monotonia
def test_monotonia_budget(build, by_name):
    finals = []
    for budget in (50, 60, 100):
        a = build(budget=budget)
        mb = v.max_bid_breakdown(a, by_name(a, "ALPHA"), "IO")
        finals.append(mb["final"])
    assert finals[0] <= finals[1] <= finals[2]
    # valori esatti quando i cap riserva/ruolo crescono col budget
    assert finals[0] == 22 and finals[1] == 32


def test_monotonia_riserva_dopo_acquisto():
    pool = make_a_pool(6)
    a = wa_module_aux(pool, teams=1, budget=60)
    p1 = a.find("A01")
    mb0 = v.max_bid_breakdown(a, p1, "IO")
    assert mb0["binding_cap"] == "reserve_cap"
    a.mark_sold(p1, mb0["final"], "IO")  # spende fino al cap riserva
    mb1 = v.max_bid_breakdown(a, a.find("A02"), "IO")
    assert mb1["reserve_cap"] < mb0["reserve_cap"]
    assert mb1["final"] <= mb0["final"]
    assert mb1["final"] >= 0
    assert_breakdown_consistente(mb1)


# -------------------------------------------------- suggested invariato (WP6)
def test_suggested_identico_pre_wp6_in_stati_misti(build, by_name):
    a = build()
    disc = 1.0
    # stato iniziale
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        p = by_name(a, nome)
        e = a.evaluate(p, "IO")
        expected = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
        assert e["suggested"] == expected
        assert e["maxbid_breakdown"]["suggested"] == expected
    # dopo vendite ALTRO + invenduti (sconto)
    a.mark_sold(by_name(a, "BETA"), 40, None)
    a.mark_unsold(by_name(a, "ALPHA"))
    a.mark_unsold(by_name(a, "ALPHA"))
    for nome in ("ALPHA", "GAMMA", "DELTA"):
        p = by_name(a, nome)
        disc = a.cfg["unsold_discount"] ** a.state["unsold"].get(p["pid"], 0)
        e = a.evaluate(p, "IO")
        expected = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
        assert e["suggested"] == expected


# ------------------------------------------------------------- bounds pool
def test_breakdown_bounded_su_tutto_il_pool_dopo_vendite(build, by_name):
    pool = []
    for role, n in (("P", 6), ("D", 12), ("C", 12), ("A", 8)):
        pool += [
            mk(f"{role}{i:02d}", ruolo=role, pfc=40.0 + i, slot=1 + (i % 3))
            for i in range(1, n + 1)
        ]
    a = build(players=pool, budget=300)
    a.mark_sold(by_name(a, "A01"), 90, "IO")
    a.mark_sold(by_name(a, "D01"), 80, "T1")
    a.mark_sold(by_name(a, "C01"), 70, "T2")
    a.mark_sold(by_name(a, "P01"), 50, None)  # ALTRO
    assert a.check_invariants() == []
    for pid in a.state["pool"]:
        p = a.players[pid]
        for team in ("IO", "T1", "T2", "ALTRO"):
            e = a.evaluate(p, team)
            mb = e["maxbid_breakdown"]
            assert_breakdown_consistente(mb)
            assert e["maxbid"] == mb["final"]
            if mb["final"] is None:
                continue
            assert mb["final"] == min(mb[k] for k in CAP_KEYS if mb[k] is not None)
            # conseguenza della definizione: il final non rende impossibile la
            # rosa (reserve_cap >= final) e sta dentro il tetto di mercato
            assert mb["final"] <= mb["reserve_cap"]
            assert mb["final"] <= mb["market_cap"]


# ------------------------------------------------------------ CLI format
def test_format_eval_mostra_breakdown(build, by_name, capsys=None):
    a = build()
    out = a.format_eval(by_name(a, "ALPHA"), "IO")
    assert "Max offerta IO:" in out
    for tok in ("cap mercato", "cap riserva", "cap ruolo", "cap opportunità"):
        assert tok in out, tok
    # coperto: nessun breakdown, nessun crash
    pool = [mk(f"A{i:02d}") for i in range(1, 7)]
    a2 = build(players=pool, teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a2.mark_sold(by_name(a2, "A01"), 10, "IO")
    out2 = a2.format_eval(by_name(a2, "A02"), "IO")
    assert "Max offerta" not in out2 or "esauriti" in out2


# ------------------------------------------------------------ payload API web
def test_web_payload_maxbid_breakdown():
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
    mb = payload["maxbid_breakdown"]
    assert mb is not None
    assert payload["maxbid"] == mb["final"] == mb["maxbid"]
    for k in CAP_KEYS + ("final", "binding_cap", "role", "alternative", "candidate"):
        assert k in mb, k
    # anche con team=None (default io) il breakdown è propagato
    payload2 = wa.player_payload(wa.engine, p, None)
    assert payload2["maxbid_breakdown"]["final"] == mb["final"]
    # e un giocatore venduto non esplode: errore, nessuna chiave numerica
    wa.engine.mark_sold(p, 50, "IO")
    payload3 = wa.player_payload(wa.engine, wa.engine.find("A02"), None)
    assert payload3["maxbid_breakdown"] is not None


def wa_module_aux(pool, **over):
    """Auction di test senza la fixture build (per config/mix custom)."""
    from openfanta.core.auction import Auction  # pyright: ignore[reportMissingImports]

    kwargs = {"teams": 3, "budget": 100, "slots": dict(DEFAULT_SLOTS)}
    kwargs.update(over)
    slots = kwargs["slots"]
    kwargs["formation"] = {
        r: min(DEFAULT_FORMATION[r], slots[r]) for r in DEFAULT_FORMATION
    }
    return Auction([dict(q) for q in pool], **kwargs)
