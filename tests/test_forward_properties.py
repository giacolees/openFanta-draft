"""Proprieta' su snapshot casuali seeded (WP forward) — vedi §12.5.

Loop seeded su ``random``: snapshot casuali (teams 1-8, budget 10-500, slots
0-6, pool variabile, player base 1-400, meta' dei casi con rank) -> simulate
(n_runs=200, trace) -> invarianti §6 per ogni run, nessuna eccezione, output
finiti; determinismo byte-identico del report.
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from forward_agg import report  # pyright: ignore[reportMissingImports]
from forward_bidding import (  # pyright: ignore[reportMissingImports]
    BidConfig,
    team_infeasible,
)
from forward_sim import simulate  # pyright: ignore[reportMissingImports]
from forward_state import (  # pyright: ignore[reportMissingImports]
    state_from_snapshot,
    validate_snapshot,
)

RNG = random.Random(20260901)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def random_snapshot(rng: random.Random) -> dict:
    n_teams = rng.randint(1, 8)
    teams = []
    for i in range(n_teams):
        teams.append(
            {
                "team": f"T{i}",
                "budget": rng.randint(10, 500),
                "slots_a": rng.randint(0, 6),
                "slots_other": {},
            }
        )
    n_players = rng.randint(1, 12)
    players = []
    for i in range(n_players):
        base = rng.randint(1, 400)
        rank = (
            rng.choice([None, None, rng.randint(1, n_players)])
            if rng.random() < 0.5
            else None
        )
        # meta' dei casi con rank assegnato
        if rng.random() < 0.5:
            rank = rng.randint(1, max(1, n_players))
        players.append(
            {
                "pid": f"p{i:02d}",
                "nome": f"GIOC{i}",
                "base": base,
                "pma": _f(base * 1.05),
                "unc_pfc": rng.choice([None, None, round(rng.uniform(1, 40), 1)]),
                "suggested0": _f(max(2, base * rng.uniform(0.8, 1.4))),
                "scarc0": round(rng.uniform(0.6, 3.0), 2),
                "unsold0": rng.randint(0, 3),
                "value": None if rank is None else _f(base * rng.uniform(0.9, 1.5)),
                "rank": rank,
            }
        )
    # pool variabile: sottoinsieme dei giocatori (mai pid inventati)
    pool = [p["pid"] for p in players]
    rng.shuffle(pool)
    keep = rng.randint(0, len(pool))
    pool = pool[:keep]
    if not pool and n_teams > 0:
        pool = [players[0]["pid"]]
    return {
        "schema_version": 1,
        "teams": teams,
        "players": players,
        "pool": pool,
        "money_league": sum(t["budget"] for t in teams),
    }


def _recursive_finite(obj) -> bool:
    if isinstance(obj, dict):
        return all(_recursive_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return all(_recursive_finite(v) for v in obj)
    if isinstance(obj, (int, float)):
        return math.isfinite(obj)
    return True


def test_properties_random_snapshots():
    for i in range(30):
        snap = random_snapshot(RNG)
        assert validate_snapshot(snap, BidConfig()) == []
        cfg = BidConfig(n_runs=200, seed=RNG.randint(0, 2**31 - 1))
        try:
            res, traces = simulate(snap, cfg, trace=True)
        except Exception as e:  # mai eccezioni inattese
            raise AssertionError(f"iterazione {i}: simulate ha alzato {e!r}") from e
        total0 = state_from_snapshot(snap, cfg).money_league
        assert len(traces) == cfg.n_runs
        for tr in traces:
            spent = sum(p[4] for p in tr.purchases)
            assert sum(t.budget for t in tr.end_teams) + spent == total0
            for t in tr.end_teams:
                assert t.budget >= 0 and t.slots_a >= 0
                # invariante §6.3: squadra feasible con slot aperti -> pool vuoto
                assert not (
                    t.slots_a > 0 and tr.end_pool and not team_infeasible(t, cfg)
                )
        # report finito e deterministico
        rep_a = report(res, snap, cfg, deterministic_report=True)
        rep_b = report(simulate(snap, cfg), snap, cfg, deterministic_report=True)
        j_a = json.dumps(rep_a, sort_keys=True)
        j_b = json.dumps(rep_b, sort_keys=True)
        assert j_a == j_b, f"iterazione {i}: report non deterministico"
        assert _recursive_finite(rep_a), f"iterazione {i}: valori non finiti nel report"


def test_properties_pool_edge_invariants():
    """Pool vuoto o piu' squadre di giocatori: mai crash, output coerente."""
    snap = {
        "schema_version": 1,
        "teams": [{"team": "T0", "budget": 100, "slots_a": 1, "slots_other": {}}],
        "players": [
            {
                "pid": "p00",
                "nome": "X",
                "base": 30,
                "pma": 31.0,
                "unc_pfc": 2.0,
                "suggested0": 33.0,
                "scarc0": 1.0,
                "unsold0": 0,
                "value": 40.0,
                "rank": 1,
            }
        ],
        "pool": [],
        "money_league": 100,
    }
    cfg = BidConfig(n_runs=50, seed=1)
    res, traces = simulate(snap, cfg, trace=True)
    for tr in traces:
        assert tr.purchases == ()
    rep = report(res, snap, cfg, deterministic_report=True)
    assert rep["feasibility"]["shortfall"] >= 0
    assert _recursive_finite(rep)

    # squadre tutte infeasible -> niente vendite e warning, mai crash
    snap2 = {
        "schema_version": 1,
        "teams": [
            {"team": "T0", "budget": 1, "slots_a": 3, "slots_other": {}},
            {"team": "T1", "budget": 2, "slots_a": 4, "slots_other": {}},
        ],
        "players": [
            {
                "pid": "p00",
                "nome": "Y",
                "base": 30,
                "pma": 31.0,
                "unc_pfc": 2.0,
                "suggested0": 33.0,
                "scarc0": 1.0,
                "unsold0": 0,
                "value": None,
                "rank": None,
            }
        ],
        "pool": ["p00"],
        "money_league": 3,
    }
    res2, traces2 = simulate(snap2, BidConfig(n_runs=50, seed=2), trace=True)
    for tr in traces2:
        assert tr.purchases == ()
    rep2 = report(res2, snap2, BidConfig(n_runs=50, seed=2), deterministic_report=True)
    assert rep2["feasibility"]["teams_infeasible"]
    assert _recursive_finite(rep2)
