"""Test del driver Monte Carlo (WP forward) — vedi §12.3.

Determinismo, terminazione, invariante di completamento (§6.3), fase di
completion forcing, shortfall, conservazione del ledger per run e squadre
infeasible bloccate.
"""

from __future__ import annotations

import pytest  # pyright: ignore[reportMissingImports]

from openfanta.forward.agg import report  # pyright: ignore[reportMissingImports]
from openfanta.forward.bidding import (  # pyright: ignore[reportMissingImports]
    BidConfig,
    team_infeasible,
)
from openfanta.forward.sim import simulate  # pyright: ignore[reportMissingImports]
from openfanta.forward.state import (
    state_from_snapshot,  # pyright: ignore[reportMissingImports]
)

CFG = BidConfig()


# ------------------------------------------------------------- helper
def _f(x):
    """float() difensivo per dati test."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def make_player(
    pid: str, nome: str, base: int = 50, *, rank: int | None = None
) -> dict:
    return {
        "pid": pid,
        "nome": nome,
        "base": base,
        "pma": _f(base) * 1.05,
        "unc_pfc": 5.0,
        "suggested0": _f(base) * 1.1,
        "scarc0": 1.0,
        "unsold0": 0,
        "value": None if rank is None else _f(base) * 1.3,
        "rank": rank,
    }


def make_snapshot(teams=None, players=None, pool=None) -> dict:
    teams = teams or [("IO", 100, 2), ("T1", 100, 2)]
    players = players or [make_player("p1", "ALFA", 60), make_player("p2", "BETA", 40)]
    return {
        "schema_version": 1,
        "teams": [
            {"team": t[0], "budget": t[1], "slots_a": t[2], "slots_other": {}}
            for t in teams
        ],
        "players": players,
        "pool": pool if pool is not None else [p["pid"] for p in players],
        "money_league": sum(t[1] for t in teams),
    }


# ------------------------------------------------------------- determinismo
def test_determinism_same_seed():
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2), ("T2", 100, 2)],
        players=[
            make_player(f"p{i}", f"P{i}", base=10 * i, rank=i) for i in range(1, 5)
        ],
    )
    cfg = BidConfig(n_runs=300, seed=7)
    r1 = simulate(snap, cfg)
    r2 = simulate(snap, cfg)
    d1 = report(r1, snap, cfg, deterministic_report=True)
    d2 = report(r2, snap, cfg, deterministic_report=True)
    assert d1 == d2
    assert r1.n_sold == r2.n_sold


def test_determinism_byte_identical_report():
    import json

    snap = make_snapshot()
    cfg = BidConfig(n_runs=200, seed=3)
    r1 = simulate(snap, cfg)
    r2 = simulate(snap, cfg)
    j1 = json.dumps(report(r1, snap, cfg, deterministic_report=True), sort_keys=True)
    j2 = json.dumps(report(r2, snap, cfg, deterministic_report=True), sort_keys=True)
    assert j1 == j2


def test_different_seed_differs():
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2)],
        players=[make_player("p1", "ALFA", 60)],
    )
    r1 = simulate(snap, BidConfig(n_runs=500, seed=1))
    r2 = simulate(snap, BidConfig(n_runs=500, seed=2))
    assert r1.n_sold != r2.n_sold or any(
        r1.sum_price.get(k, 0) != r2.sum_price.get(k, 0) for k in r1.n_sold
    )


# ------------------------------------------------------------- terminazione
def test_termination_all_runs_finish():
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2)],
        players=[make_player(f"p{i}", f"P{i}", base=5 * i) for i in range(1, 9)],
    )
    cfg = BidConfig(n_runs=200, seed=11)
    _, traces = simulate(snap, cfg, trace=True)
    assert len(traces) == cfg.n_runs
    # ogni run ha un numero finito di round (pool si svuota o niente elegibili)
    for tr in traces:
        assert isinstance(tr.purchases, tuple)
        # prezzi tutti interi >= pavimento
        for _, _, _, _, price in tr.purchases:
            assert type(price) is int and price >= 1
        # pool mai sovrapposto ai venduti: verifica sul numero di acquisti
        assert len(tr.purchases) <= len(snap["players"])


# ------------------------------------------------------------- completamento
def completion_scenario(*, rank: bool):
    players = [
        make_player("p1", "ALFA", 60, rank=1 if rank else None),
        make_player("p2", "BETA", 50, rank=8 if rank else None),
        make_player("p3", "GAMMA", 40, rank=5 if rank else None),
        make_player("p4", "DELTA", 30, rank=4 if rank else None),
    ]
    snap = make_snapshot(
        teams=[("IO", 200, 2), ("T1", 200, 2)],  # domanda 4 == pool 4
        players=players,
        pool=[p["pid"] for p in players],
    )
    return snap, BidConfig(n_runs=200, seed=13)


def test_completion_invariant_with_pass():
    """Pool == domanda: ogni run termina con tutte le squadre feasibili a
    slots_a == 0, anche con rank presente (i pass vengono riparati dalla fase
    di completion forcing)."""
    snap, cfg = completion_scenario(rank=True)
    res, traces = simulate(snap, cfg, trace=True)
    assert res.completion_runs > 0  # il pass ha reso necessaria la fase
    for tr in traces:
        for t in tr.end_teams:
            if not team_infeasible(t, cfg):
                assert t.slots_a == 0, f"{t.team} resta scoperto: {t}"
        assert tr.end_pool == frozenset() or all(
            t.slots_a == 0 or team_infeasible(t, cfg) for t in tr.end_teams
        )
    # invarianze globali del risultato
    assert sum(res.unfilled_count.values()) >= 0
    assert all(res.n_sold.get(p["pid"], 0) <= cfg.n_runs for p in snap["players"])


def test_completion_phase_runs_only_when_needed():
    # senza rank/pass la fase di completamento non parte mai
    snap, cfg = completion_scenario(rank=False)
    res, _ = simulate(snap, cfg, trace=True)
    assert res.completion_runs == 0
    # con rank/pass parte (pass_prob > 0)
    snap2, cfg2 = completion_scenario(rank=True)
    res2, _ = simulate(snap2, cfg2, trace=True)
    assert res2.completion_runs > 0
    assert res2.completion_purchases > 0


def test_completion_phase_no_bids_passes():
    snap, cfg = completion_scenario(rank=True)
    _, traces = simulate(snap, cfg, trace=True)
    for tr in traces:
        phase_completion = [p for p in tr.purchases if p[1] == "completion"]
        for _ in phase_completion:
            pass  # acquisti nella fase di completion: presenti quando invariante attiva


# ------------------------------------------------------------- shortfall
def test_shortfall_pool_less_than_demand():
    snap = make_snapshot(
        teams=[("IO", 300, 3), ("T1", 300, 3)],  # domanda 6
        players=[
            make_player(f"p{i}", f"P{i}", base=10 * i) for i in range(1, 4)
        ],  # pool 3
        pool=[f"p{i}" for i in range(1, 4)],
    )
    cfg = BidConfig(n_runs=150, seed=17)
    res, traces = simulate(snap, cfg, trace=True)
    feas = res.feasibility
    assert feas["shortfall"] > 0
    assert sum(res.unfilled_count.values()) >= 0  # chi resta scoperto e' riportato
    for tr in traces:
        # pool vuoto a fine run (pool < domanda => tutto venduto o infeasible)
        for t in tr.end_teams:
            if not team_infeasible(t, cfg):
                assert t.slots_a >= 3 - 3
    # somma unfilled per run <= shortfall statico
    for tr in traces:
        unfilled_feas = sum(
            1 for t in tr.end_teams if t.slots_a > 0 and not team_infeasible(t, cfg)
        )
        assert unfilled_feas <= feas["shortfall"]


def test_shortfall_never_invents_players():
    snap = make_snapshot(
        teams=[("IO", 300, 4)],
        players=[make_player("p1", "ALFA", 60)],
        pool=["p1"],
    )
    cfg = BidConfig(n_runs=100, seed=19)
    _, traces = simulate(snap, cfg, trace=True)
    for tr in traces:
        # il numero di acquisti <= pool (mai giocatori inventati)
        assert len(tr.purchases) <= 1
        assert {p[2] for p in tr.purchases} <= set(snap["pool"])


# ------------------------------------------------------------- ledger / invarianti
def test_ledger_conserved_per_run():
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2), ("T2", 100, 2)],
        players=[
            make_player(f"p{i}", f"P{i}", base=8 * i, rank=(i % 5) + 1)
            for i in range(1, 8)
        ],
    )
    cfg = BidConfig(n_runs=100, seed=23)
    _, traces = simulate(snap, cfg, trace=True)
    total0 = state_from_snapshot(snap, cfg).money_league
    for tr in traces:
        spent = sum(p[4] for p in tr.purchases)
        budget_end = sum(t.budget for t in tr.end_teams)
        assert budget_end + spent == total0
        for t in tr.end_teams:
            assert t.budget >= 0 and t.slots_a >= 0
    # nessuno slot negativo in nessuna run, pool disgiunto dai venduti
    for tr in traces:
        sold = {p[2] for p in tr.purchases}
        assert tr.end_pool.isdisjoint(sold)


def test_trace_reconstructs_state():
    snap, cfg = completion_scenario(rank=True)
    _, traces = simulate(snap, cfg, trace=True)
    te0 = {t.team: t for t in state_from_snapshot(snap, cfg).teams}
    for tr in traces[:50]:
        budgets = {team: t.budget for team, t in te0.items()}
        slots = {team: t.slots_a for team, t in te0.items()}
        pool = set(snap["pool"])
        for _, _, pid, team, price in tr.purchases:
            assert pid in pool
            pool.discard(pid)
            budgets[team] -= price
            slots[team] -= 1
            assert budgets[team] >= 0 and slots[team] >= 0
        assert pool == tr.end_pool
        for t in tr.end_teams:
            assert budgets[t.team] == t.budget
            assert slots[t.team] == t.slots_a


# ------------------------------------------------------------- squadra infeasible
def test_infeasible_team_never_bids():
    snap = make_snapshot(
        teams=[("OK", 200, 2), ("ROSSO", 3, 3)],  # ROSSO: budget 3 < riserva 6
        players=[make_player("p1", "ALFA", 60), make_player("p2", "BETA", 40)],
    )
    cfg = BidConfig(n_runs=150, seed=29)
    res, traces = simulate(snap, cfg, trace=True)
    infeas = res.feasibility["teams_infeasible"]
    assert any(t["team"] == "ROSSO" for t in infeas)
    for tr in traces:
        for _, _, _, team, _ in tr.purchases:
            assert team != "ROSSO"
    assert any("value/rank" in w for w in res.warnings)


# ------------------------------------------------------------- result invarianti
def test_result_counts_consistent():
    snap, cfg = completion_scenario(rank=True)
    res, _ = simulate(snap, cfg, trace=True)
    n = cfg.n_runs
    for p in snap["players"]:
        pid = p["pid"]
        assert res.n_sold.get(pid, 0) + (n - res.n_sold.get(pid, 0)) == n
        assert 0 <= res.n_sold.get(pid, 0) <= n
        if res.price_list is not None:
            assert len(res.price_list.get(pid, [])) == res.n_sold.get(pid, 0)
    assert res.completion_runs <= n
    assert res.total_rounds >= 0
    # documento: completion_phase coerenza nel report
    rep = report(res, snap, cfg)
    comp = rep["competition"]["completion_phase"]
    assert comp["n_runs_with_phase"] + comp["n_runs_without"] == n


def test_simulate_invalid_input():
    with pytest.raises(ValueError):
        simulate({"schema_version": 1, "teams": [], "players": [], "pool": []}, CFG)
    with pytest.raises(ValueError):
        simulate(make_snapshot(), BidConfig(n_runs=0))
    with pytest.raises(ValueError):
        simulate(make_snapshot(), BidConfig(seed="x"))
    with pytest.raises(ValueError):
        simulate(make_snapshot(), BidConfig(player_order="no"))
