"""Test dell'aggregazione/report (WP forward) — vedi §12.4.

Probabilita' team-player, top rosters, contratto (chiavi del report),
separazione market/model_value/divergence e determinismo byte-identico.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from forward_agg import report  # pyright: ignore[reportMissingImports]
from forward_bidding import BidConfig  # pyright: ignore[reportMissingImports]
from forward_sim import simulate  # pyright: ignore[reportMissingImports]

CFG = BidConfig()


def _f(x):
    """float() difensivo per dati test."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def make_player(
    pid: str,
    nome: str,
    base: int = 50,
    *,
    rank: int | None = None,
    value: float | None = None,
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
        "value": _f(base) * 1.3 if value is None else value,
        "rank": rank,
    }


def make_snapshot(teams=None, players=None) -> dict:
    teams = teams or [("IO", 150, 2), ("T1", 150, 2), ("T2", 150, 2)]
    players = players or [
        make_player("p1", "ALFA", 60, rank=1),
        make_player("p2", "BETA", 45, rank=2),
        make_player("p3", "GAMMA", 30, rank=3),
        make_player("p4", "DELTA", 20, rank=4),
        make_player("p5", "EPSIL", 12, rank=5),
        make_player("p6", "ZETA", 6),
    ]
    return {
        "schema_version": 1,
        "teams": [
            {"team": t[0], "budget": t[1], "slots_a": t[2], "slots_other": {}}
            for t in teams
        ],
        "players": players,
        "pool": [p["pid"] for p in players],
        "money_league": sum(t[1] for t in teams),
    }


def _report(snapshot: dict | None = None, cfg: BidConfig | None = None) -> dict:
    snap = snapshot or make_snapshot()
    cfg = cfg or CFG
    res = simulate(snap, cfg)
    return report(res, snap, cfg, deterministic_report=True)


# ------------------------------------------------------------- core numbers
def test_avg_price_and_sale_prob():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=500, seed=5)
    rep = _report(snap, cfg)
    n = cfg.n_runs
    res = simulate(snap, cfg)
    for row in rep["per_player"]:
        pid = row["pid"]
        n_sold = res.n_sold.get(pid, 0)
        assert row["n_sold"] == n_sold
        assert row["n_unsold"] == n - n_sold
        assert row["sale_prob"] is not None
        assert 0.0 <= row["sale_prob"] <= 1.0
        if n_sold:
            want = res.sum_price[pid] / n_sold
            assert math.isclose(row["avg_price"] or 0.0, round(want, 1), abs_tol=0.06)
        else:
            assert row["avg_price"] is None


def test_team_player_sum_matches_sale_prob():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=300, seed=6)
    rep = _report(snap, cfg)
    by_pid: dict[str, float] = {}
    for row in rep["team_player_prob"]:
        by_pid[row["pid"]] = by_pid.get(row["pid"], 0.0) + row["prob"]
    tol = 0.005 + 0.001 * len(snap["teams"])  # rounding per riga (3 decimali)
    for p in snap["players"]:
        pid = p["pid"]
        sale_prob = rep["per_player"][
            next(i for i, r in enumerate(rep["per_player"]) if r["pid"] == pid)
        ]["sale_prob"]
        assert math.isclose(by_pid.get(pid, 0.0), sale_prob or 0.0, abs_tol=tol)
        assert -0.001 <= by_pid.get(pid, 0.0) <= 1.0 + tol


def test_top_rosters_freq_plus_unfilled_equals_runs():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=200, seed=7)
    rep = _report(snap, cfg)
    res = simulate(snap, cfg)
    n = cfg.n_runs
    for t in snap["teams"]:
        team = t["team"]
        top = rep["per_team"][
            next(i for i, r in enumerate(rep["per_team"]) if r["team"] == team)
        ]
        freq_sum = sum(r["freq"] for r in top["top_rosters"])
        assert (
            freq_sum + top["unfilled_prob"] * n <= n + 1e-6
        )  # freq dei top-k + unfilled
        # invarianza esatta sui conteggi raw: freq totali (tutte le combo) + unfilled == n
        raw_freq = sum(res.combos[team].values())
        assert raw_freq + res.unfilled_count[team] == n
        # slot filled medio dentro [0, slots_a0]
        assert 0.0 <= top["slots_filled_avg"] <= t["slots_a"] + 1e-9
        assert 0.0 <= top["unfilled_prob"] <= 1.0


def test_slots_filled_avg_bounds():
    snap = make_snapshot(teams=[("IO", 400, 2)])
    cfg = BidConfig(n_runs=150, seed=8)
    rep = _report(snap, cfg)
    row = rep["per_team"][0]
    assert row["slots_filled_avg"] <= 2.0 + 1e-9
    assert row["budget_end_avg"] >= 0


# ------------------------------------------------------------- contratto
def test_contract_keys_and_completion_counts():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=200, seed=9)
    rep = _report(snap, cfg)
    n = cfg.n_runs
    for row in rep["per_player"]:
        assert "n_passed" in row
        assert "n_unsold" in row
        assert set(row["market"]) == {
            "base",
            "pma",
            "suggested_snapshot",
            "scarcity_snapshot",
            "sim_avg_price",
        }
        assert set(row["model_value"]) == {"value", "rank", "source"}
        assert set(row["divergence"]) == {
            "sim_minus_base",
            "sim_minus_pma",
            "sim_minus_value",
        }
    comp = rep["competition"]
    assert "completion_phase" in comp
    cp = comp["completion_phase"]
    assert cp["n_runs_with_phase"] + cp["n_runs_without"] == n
    assert "per_band" in comp
    assert rep["league"]["money_league"] == sum(t["budget"] for t in snap["teams"])
    assert "snapshot_hash" in rep and rep["seed"] == cfg.seed
    assert rep["schema_version"] == 1


def test_market_model_divergence_separated():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=250, seed=10)
    rep = _report(snap, cfg)
    for row in rep["per_player"]:
        # il blocco model_value e' solo input, mai derivato dal prezzo simulato
        mv = row["model_value"]
        assert mv["source"] == "input"
        # value e rank non compaiono in market
        assert "value" not in row["market"]
        assert "rank" not in row["market"]
        # nessuna chiave composta: i delta sono etichettati per coppia
        for k in row["divergence"]:
            assert k.startswith("sim_minus_")
        # divergence coerente con avg_price
        avg = row["avg_price"]
        if avg is not None:
            assert math.isclose(
                row["divergence"]["sim_minus_base"] or 0,
                round(avg - row["market"]["base"], 1),
                abs_tol=0.1,
            )


def test_percentiles_present_when_collect():
    snap = make_snapshot()
    rep = _report(snap, BidConfig(n_runs=200, seed=11, collect_prices=True))
    sold_any = [r for r in rep["per_player"] if r["n_sold"] > 0]
    assert sold_any
    for row in sold_any:
        assert "p10_price" in row and "p50_price" in row and "p90_price" in row
        assert row["p10_price"] <= row["p50_price"] <= row["p90_price"]
    rep_off = _report(snap, BidConfig(n_runs=200, seed=12, collect_prices=False))
    for row in rep_off["per_player"]:
        assert "p10_price" not in row


def test_all_values_finite():
    snap = make_snapshot(
        players=[
            make_player(f"p{i}", f"P{i}", base=3 * i, rank=i) for i in range(1, 7)
        ],
    )
    cfg = BidConfig(n_runs=120, seed=13)
    rep = _report(snap, cfg)

    def check(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                check(v)
        elif isinstance(obj, list):
            for v in obj:
                check(v)
        elif isinstance(obj, (int, float)):
            assert math.isfinite(obj)

    check(rep)


# ------------------------------------------------------------- determinismo
def test_deterministic_report_no_timestamp_and_byte_identical():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=150, seed=14)
    r1 = simulate(snap, cfg)
    r2 = simulate(snap, cfg)
    rep1 = report(r1, snap, cfg, deterministic_report=True)
    rep2 = report(r2, snap, cfg, deterministic_report=True)
    assert "generated_at" not in rep1
    j1 = json.dumps(rep1, sort_keys=True)
    j2 = json.dumps(rep2, sort_keys=True)
    assert j1 == j2
    # report() non emette MAI timestamp (lo aggiunge la CLI solo se richiesto):
    # il flag deterministic_report e' un riequilibrio per contratto/byte-identity
    rep_false = report(r1, snap, cfg, deterministic_report=False)
    assert "generated_at" not in rep_false
    assert json.dumps(rep_false, sort_keys=True) == j1


def test_rounding_deterministic_identity_inputs():
    snap = make_snapshot()
    cfg = BidConfig(n_runs=100, seed=15)
    a = _report(snap, cfg)
    b = _report(json.loads(json.dumps(snap)), cfg)
    assert a == b
