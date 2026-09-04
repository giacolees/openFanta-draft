"""Test del modulo di valutazione per-singola-squadra (openfanta.core.valuation) — WP5.

Coprono la scarsità per ruolo e per singola squadra, separata in quattro
componenti (quantitativa, qualitativa, economica, competizione):

- riserva minima completamento rosa (``reserve_floor``) e budget contendibile
  (``contendible_budget``), ALTRO mai simulata;
- squadre effettivamente concorrenti (``competing_teams``): slot aperto nel
  ruolo E budget contendibile >= soglia (PFC del candidato o floor del ruolo);
- ``scarcity_breakdown``: conteggi/rapporti grezzi + fattori finiti e bounded
  nei clamp di config; team coperto ⇒ componente team neutra 1 e flag covered;
  monotonia controllata (meno alternative comparabili ⇒ più pressione;
  concorrenti senza budget non contano; slot coperti riducono la competizione;
  più budget contendibile non riduce la pressione economica);
- ``suggested`` e ``maxbid`` invariati da WP5 (backward compat di evaluate);
- payload API web che propaga i nuovi campi;
- input patologici ma raggiungibili (venduti, slot ruolo zero, pool vuoto,
  budget minimo) e bounds su TUTTO il pool dopo una sequenza di vendite.
"""

import math

import pytest  # pyright: ignore[reportMissingImports]
from conftest import (
    DEFAULT_FORMATION,
    DEFAULT_SLOTS,
    make_player,
)

import openfanta.web.app as wa  # pyright: ignore[reportMissingImports]
from openfanta.core import valuation  # pyright: ignore[reportMissingImports]

FACTOR_KEYS = ("quant", "qual", "econ", "competition")


def assert_bounded(a, bd):
    """Tutti i fattori del breakdown finiti e dentro i clamp di config."""
    for key in FACTOR_KEYS:
        f = bd[key]["factor"]
        assert math.isfinite(f), f"{key} non finito"
        assert a.cfg["scarcity_min"] <= f <= a.cfg["scarcity_max"], f"{key} fuori clamp"
    assert math.isfinite(bd["final"])
    assert a.cfg["scarcity_min"] <= bd["final"] <= a.cfg["scarcity_max"]


def make_pool(nome, ruolo="A", pfc=50.0, slot=2, expfm=6.5, **kw):
    """make_player di conftest con expfm configurabile (qualità simile)."""
    p = make_player(nome, ruolo=ruolo, pfc=pfc, slot=slot, **kw)
    p["expfm"] = expfm  # gia' numerico (float) dallo schema
    return p


# ------------------------------------------------------------ reserve_floor
def test_reserve_floor_default(build):
    a = build()
    r = valuation.reserve_floor(a, "IO")
    assert r["per_role"] == {"P": 6, "D": 6, "C": 6, "A": 12}
    assert r["total"] == 30  # 15 slot x floor 2


def test_reserve_floor_scala_dopo_vendita(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 10, "IO")
    assert valuation.reserve_floor(a, "IO")["total"] == 28  # A: 6 -> 5
    assert valuation.reserve_floor(a, "T1")["total"] == 30  # T1 intatta


def test_reserve_floor_altro_zero(build):
    a = build()
    r = valuation.reserve_floor(a, "ALTRO")
    assert set(r["per_role"]) == {"P", "D", "C", "A"}
    assert r["total"] == 0  # ALTRO non ha rosa: nessuno slot inventato


def test_reserve_floor_floor_per_ruolo(build):
    a = build(role_floor_price={"P": 3, "D": 2, "C": 2, "A": 4})
    assert valuation.reserve_floor(a, "IO")["total"] == 9 + 6 + 6 + 24  # 45


# --------------------------------------------------------- contendible_budget
def test_contendible_mai_negativo(build):
    a = build(budget=10)
    assert valuation.contendible_budget(a, "IO") == 0  # riserva 30 > budget
    assert valuation.contendible_budget(a, "ALTRO") == 0
    assert valuation.contendible_budget(a, "ROMA") == 0  # non tracciata
    a2 = build(budget=100)
    assert valuation.contendible_budget(a2, "IO") == 70  # 100 - 30


# ---------------------------------------------------------- competing_teams
def test_competing_teams_con_candidato(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")  # base 50
    comps = valuation.competing_teams(a, "A", p)
    assert [c["team"] for c in comps] == ["IO", "T1", "T2"]
    assert all(c["money"] == 100 and c["reserve"] == 30 for c in comps)
    assert all(c["contendible"] == 70 for c in comps)  # >= 50


def test_competing_teams_senza_candidato_floor(build):
    a = build()
    # senza candidato la soglia è il floor del ruolo (2): tutti competono
    assert len(valuation.competing_teams(a, "A")) == 3


def test_competing_teams_candidato_costoso_nessuno(build, by_name):
    pool = [make_pool(n, pfc=200.0) for n in ("ALPHA", "BETA", "GAMMA", "DELTA")]
    a = build(players=pool)
    p = by_name(a, "ALPHA")  # base 200 > contendible 70
    assert valuation.competing_teams(a, "A", p) == []


def test_concorrenti_senza_budget_non_contano(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 100, "T2")  # T2: budget 0, contendible 0
    p = by_name(a, "GAMMA")
    comps = valuation.competing_teams(a, "A", p)
    assert len(comps) == 2
    assert "T2" not in [c["team"] for c in comps]
    assert [c["team"] for c in comps] == ["IO", "T1"]


def test_competing_teams_slot_coperto_escluso(build, by_name):
    # squadra senza slot aperti nel ruolo: esclusa anche con budget pieno
    pool = [make_pool(n) for n in ("ALPHA", "BETA", "GAMMA", "DELTA")]
    a = build(players=pool, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "ALPHA"), 10, "T1")  # T1 copre l'unico slot A
    p = by_name(a, "BETA")
    comps = valuation.competing_teams(a, "A", p)
    assert "T1" not in [c["team"] for c in comps]
    assert len(comps) == 2


# -------------------------------------------------------- scarcity_breakdown
def test_breakdown_chiavi_raw_e_fattori_bounded(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    bd = valuation.scarcity_breakdown(a, p, "IO")
    assert bd["role"] == "A" and bd["team"] == "IO"
    assert bd["tracked"] and not bd["covered"]
    assert bd["altro_spent"] == 0
    assert set(bd) >= {
        "role",
        "team",
        "tracked",
        "covered",
        "league",
        "quant",
        "qual",
        "econ",
        "competition",
        "team_component",
        "altro_spent",
        "final",
    }
    # league: scarsità lega attuale (invariata)
    assert bd["league"]["factor"] == pytest.approx(a.scarcity(p))
    # quant grezza: stato iniziale = rapporto 1
    assert bd["quant"] == {
        "factor": 1.0,
        "ratio": 1.0,
        "demand": 18,
        "demand0": 18,
        "alt": 4,
        "alt0": 4,
    }
    # qual grezza: 3 alternative di qualità simile (escluso il candidato)
    assert bd["qual"]["count0"] == 3 and bd["qual"]["count"] == 3
    assert bd["qual"]["quality0"] == pytest.approx(19.5)
    assert bd["qual"]["quality"] == pytest.approx(19.5)
    assert bd["qual"]["ratio"] == pytest.approx(1.0)
    # econ grezza: 3 concorrenti, contendible medio 70, ratio 70/50
    assert bd["econ"]["competitors"] == 3
    assert bd["econ"]["mean_contendible"] == pytest.approx(70.0)
    assert bd["econ"]["base"] == 50
    assert bd["econ"]["ratio"] == pytest.approx(1.4)
    # competition: 3 su 3 (tutte le squadre tracciate competono)
    assert bd["competition"] == {
        "factor": 1.0,
        "ratio": 1.0,
        "competitors": 3,
        "total": 3,
        "teams": ["IO", "T1", "T2"],
    }
    # team_component = clamp(1 x 1 x 1.4^0.5); final = clamp(1.0 x team)
    assert bd["team_component"] == pytest.approx(1.4**0.5, rel=1e-3)
    assert bd["final"] == pytest.approx(1.4**0.5, rel=1e-3)
    assert_bounded(a, bd)


def test_breakdown_team_default_io(build, by_name):
    a = build()
    assert valuation.scarcity_breakdown(a, by_name(a, "ALPHA")) == (
        valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    )


def test_breakdown_coperto_team_componente_neutra(build, by_name):
    pool = [make_pool(f"A{i:02d}") for i in range(1, 9)]  # 8 A da base 50
    a = build(players=pool)
    for nome in (f"A{i:02d}" for i in range(1, 7)):
        a.mark_sold(by_name(a, nome), 10, "IO")  # IO copre A (6 -> 0)
    p = by_name(a, "A07")
    e = a.evaluate(p, "IO")
    assert e["covered"]
    bd = valuation.scarcity_breakdown(a, p, "IO")
    assert bd["covered"] and bd["tracked"]
    assert bd["team_component"] == 1.0  # componente team neutra
    assert bd["final"] == pytest.approx(round(e["scarc"], 4))  # = scarsità lega
    assert e["scarcity_team"] == bd["final"]
    assert e["scarcity_breakdown"]["final"] == bd["final"]
    assert_bounded(a, bd)


def test_breakdown_lega_una_squadra(build, by_name):
    a = build(teams=1)
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["competition"]["competitors"] == 1
    assert bd["competition"]["total"] == 1
    assert bd["competition"]["factor"] == 1.0
    assert_bounded(a, bd)


def test_breakdown_nessun_concorrente(build, by_name):
    pool = [make_pool(n, pfc=200.0) for n in ("ALPHA", "BETA", "GAMMA", "DELTA")]
    a = build(players=pool)
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["competition"]["competitors"] == 0
    assert bd["econ"]["competitors"] == 0
    assert bd["competition"]["factor"] == a.cfg["scarcity_min"]
    assert bd["econ"]["factor"] == a.cfg["scarcity_min"]
    # team = clamp(0.6 x qual(1.0) x 0.6) = 0.36 -> 0.6; league 1.0 -> final 0.6
    assert bd["team_component"] == a.cfg["scarcity_min"]
    assert bd["final"] == a.cfg["scarcity_min"]
    assert_bounded(a, bd)


def test_breakdown_altro_non_simulato(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    bd = valuation.scarcity_breakdown(a, p, "ALTRO")
    assert not bd["tracked"]
    assert not bd["covered"]  # ALTRO non ha slot simulati
    assert valuation.reserve_floor(a, "ALTRO")["total"] == 0
    assert "ALTRO" not in bd["competition"]["teams"]
    # la vendita ALTRO si contabilizza solo via stato lega
    a.mark_sold(by_name(a, "BETA"), 30, None)
    bd2 = valuation.scarcity_breakdown(a, p, "ALTRO")
    assert bd2["altro_spent"] == 30
    assert_bounded(a, bd2)


# ------------------------------------------------------------- monotonia
def test_monotonia_meno_alternative_comparabili_aumenta_pressione(build, by_name):
    a = build()
    p = by_name(a, "GAMMA")
    bd0 = valuation.scarcity_breakdown(a, p, "IO")
    a.mark_sold(by_name(a, "ALPHA"), 10, "T1")  # ALPHA comparabile a GAMMA
    bd1 = valuation.scarcity_breakdown(a, p, "IO")
    assert bd1["qual"]["factor"] > bd0["qual"]["factor"]
    assert bd1["quant"]["factor"] > bd0["quant"]["factor"]
    assert bd1["final"] > bd0["final"]
    assert_bounded(a, bd1)


def test_monotonia_slot_coperti_riducono_competition(build, by_name):
    pool = [
        make_pool("X", pfc=50.0, slot=1),  # candidato slot 1
        make_pool("Y", pfc=20.0, slot=3),  # NON comparabile a X
        make_pool("Z", pfc=50.0, slot=2),
        make_pool("W", pfc=50.0, slot=2),
        make_pool("V", pfc=50.0, slot=2),
    ]
    a = build(players=pool, budget=200)
    px = by_name(a, "X")
    bd0 = valuation.scarcity_breakdown(a, px, "IO")
    assert bd0["competition"]["competitors"] == 3
    # T1 copre (quasi) lo slot con Y: budget 200 - 190 -> contendible 0
    a.mark_sold(by_name(a, "Y"), 190, "T1")
    bd1 = valuation.scarcity_breakdown(a, px, "IO")
    assert bd1["competition"]["competitors"] == 2
    assert "T1" not in bd1["competition"]["teams"]
    assert bd1["competition"]["factor"] < bd0["competition"]["factor"]
    # Y non è un'alternativa comparabile di X: quant/qual non si muovono
    assert bd1["quant"]["alt"] == bd0["quant"]["alt"]
    assert bd1["qual"]["factor"] == bd0["qual"]["factor"]


def test_monotonia_budget_contendibile_non_riduce_economica(build, by_name):
    lo = build(budget=100)
    hi = build(budget=150)
    bd_lo = valuation.scarcity_breakdown(lo, by_name(lo, "ALPHA"), "IO")
    bd_hi = valuation.scarcity_breakdown(hi, by_name(hi, "ALPHA"), "IO")
    assert bd_hi["econ"]["ratio"] > bd_lo["econ"]["ratio"]
    assert bd_hi["econ"]["factor"] >= bd_lo["econ"]["factor"]
    # stessa competizione: la differenza è solo economica
    assert (
        bd_hi["competition"]["competitors"] == bd_lo["competition"]["competitors"] == 3
    )


# --------------------------------------- suggested/maxbid (WP6 breakdown)
# ---- WP7: contratto a tre blocchi esposto da evaluate e dal payload web ----
def test_evaluate_espone_contratto_tre_blocchi(build, by_name):
    a = build()
    e = a.evaluate(by_name(a, "ALPHA"), "IO")
    assert set(e) >= {"market", "fantasy", "my_team"}
    assert e["market"]["expected"] == e["suggested"]  # prezzo atteso == suggerito
    assert e["market"]["range"]["lo"] <= e["market"]["expected"]
    assert e["market"]["expected"] <= e["market"]["range"]["hi"]
    # fantasy: nessun prezzo; my_team: team_value separato dal maxbid
    assert "expected" not in e["fantasy"] and "pfc" not in e["fantasy"]
    assert isinstance(e["my_team"]["team_value"], int)
    assert e["maxbid"] is None or e["maxbid"] != e["my_team"]["team_value"]


def test_api_eval_espone_contratto_tre_blocchi():
    pool = [make_pool(f"A{i:02d}") for i in range(1, 7)]
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
    resp = wa.api_eval(key="A01", team="IO")
    assert isinstance(resp, dict)
    for block in ("market", "fantasy", "my_team"):
        assert block in resp and isinstance(resp[block], dict), block
    assert resp["market"]["expected"] == resp["suggested"]
    # team_value e maxbid sono valori distinti e autonomi (nessun indice unico)
    tv = resp["my_team"]["team_value"]
    assert isinstance(tv, int) and tv >= 0
    assert resp["maxbid"] == resp["maxbid_breakdown"]["final"]
    # team_value autonomo dal maxbid: nello stesso fixture 28 vs 62
    assert tv != resp["maxbid"]


def test_suggested_e_maxbid_invariati(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    e = a.evaluate(p, "IO")
    disc = 1.0  # nessun invenduto
    expected = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
    assert e["suggested"] == expected
    assert e["scarc"] == pytest.approx(a.scarcity(p))
    # WP6: maxbid = min dei 4 cap validi (mai negativo), breakdown esplicito
    mb = e["maxbid_breakdown"]
    assert e["maxbid"] == mb["final"] == mb["maxbid"]
    caps = [mb[k] for k in ("market_cap", "reserve_cap", "role_cap", "opportunity_cap")]
    assert mb["final"] == min(caps)
    assert mb["final"] >= 0
    # invarianza numerica nel caso WP5: qui il tetto storico e' ancora quello
    # che vince (market x aggression = 62, uguale al cap opportunità) -> il
    # maxbid resta IDENTICO alla formula pre-WP6 in questo stato
    slots_after = sum(a.state["slots"]["IO"].values()) - 1
    affordable = a.state["money"]["IO"] - a.cfg["min_slot_price"] * slots_after
    assert e["maxbid"] == max(0, min(round(expected * a.cfg["aggression"]), affordable))
    # dopo una vendita la formula del suggerito resta identica
    a.mark_sold(by_name(a, "BETA"), 40, "T1")
    e2 = a.evaluate(p, "IO")
    expected2 = max(1, round(p["base"] * a.inflation() * a.scarcity(p) * disc))
    assert e2["suggested"] == expected2
    mb2 = e2["maxbid_breakdown"]
    assert e2["maxbid"] == mb2["final"] == mb2["maxbid"]
    # backward compat: tutte le chiavi pre-WP5 presenti + i campi nuovi
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
        assert key in e2, f"chiave mancante: {key}"
    assert e2["scarcity_team"] == e2["scarcity_breakdown"]["final"]


# ---------------------------------------------------------- payload web API
def test_payload_web_propaga_scarsity():
    pool = [make_pool(f"A{i:02d}") for i in range(1, 7)]
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
    assert payload["scarcity_team"] is not None
    bd = payload["scarcity_breakdown"]
    for comp in FACTOR_KEYS:
        assert comp in bd
    assert payload["scarcity_team"] == bd["final"]
    # anche con team=None (default io) i campi sono propagati
    payload2 = wa.player_payload(wa.engine, p, None)
    assert payload2["scarcity_team"] == bd["final"]
    assert payload2["scarcity_breakdown"]["team"] == "IO"


# ------------------------------------------------- input patologici raggiungibili
def test_breakdown_giocatore_venduto_finito_e_bounded(build, by_name):
    a = build()
    p = by_name(a, "ALPHA")
    a.mark_sold(p, 10, "IO")
    bd = valuation.scarcity_breakdown(a, p, "IO")  # p non più nel pool
    assert_bounded(a, bd)


def test_breakdown_slot_ruolo_zero_lega_wide(build, by_name):
    a = build(teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 0})
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["covered"]
    assert bd["team_component"] == 1.0
    assert_bounded(a, bd)


def test_breakdown_pool_vuoto_finito(build, by_name):
    a = build(teams=1, budget=100, slots={"P": 3, "D": 3, "C": 3, "A": 4})
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        a.mark_sold(by_name(a, nome), 10, "IO")
    assert len(a.state["pool"]) == 0
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["covered"]  # slot A esauriti
    assert_bounded(a, bd)


def test_breakdown_budget_minimo(build, by_name):
    a = build(budget=1)
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["competition"]["competitors"] == 0
    assert_bounded(a, bd)


def test_tuning_passa_dalla_config(build, by_name):
    a = build(
        scarcity_team_beta=1.0,
        scarcity_qual_beta=1.0,
        scarcity_econ_beta=1.0,
        scarcity_qual_tol=1.0,
        scarcity_qual_base_tol=4.0,
    )
    bd = valuation.scarcity_breakdown(a, by_name(a, "ALPHA"), "IO")
    assert bd["competition"]["factor"] == 1.0  # (3/3)^1.0
    assert bd["qual"]["factor"] == 1.0  # nessuna perdita di qualità
    assert bd["econ"]["factor"] == pytest.approx(1.4)  # (70/50)^1.0
    assert bd["team_component"] == pytest.approx(1.4)
    assert_bounded(a, bd)


def test_breakdown_bounded_su_tutto_il_pool_dopo_vendite(build, by_name):
    pool = []
    for role, n in (("P", 6), ("D", 12), ("C", 12), ("A", 8)):
        pool += [
            make_pool(f"{role}{i:02d}", ruolo=role, pfc=40.0 + i, slot=1 + (i % 3))
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
            bd = valuation.scarcity_breakdown(a, p, team)
            assert_bounded(a, bd)
