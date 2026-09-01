#!/usr/bin/env -S uv run --quiet python
"""Aggregazione del simulatore Attaccanti: report JSON schema v1 (WP isolato).

``report(SimResult, snapshot, cfg)`` produce il dict JSON v1 §8 del documento:
percentili per giocatore, probabilita' team-player, top combinazioni finali per
squadra, livello di concorrenza e separazione market/model/divergence.

Regola di separazione (contratto WP7, obbligatoria):
- blocco ``market``: solo input di prezzo (base/PMA/suggested/scarcity/
  sim_avg_price);
- blocco ``model_value``: solo ``value``/``rank`` in ingresso (mai derivati
  dalla simulazione);
- ``divergence``: unico posto con i delta, etichettati per coppia
  (sim_minus_base, sim_minus_pma, sim_minus_value). Mai un indice composito.

Numeri rounded deterministicamente (``round`` di Python, round-half-even);
``deterministic_report=True`` => niente timestamp (il report e' poi
byte-identico a parita' di input). Puro Python stdlib.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from forward_bidding import BidConfig
from forward_sim import SimResult
from forward_state import band_of


# ------------------------------------------------------------- canonica/cache
def canonical_json(snapshot: dict, cfg: BidConfig) -> str:
    """Rappresentazione JSON canonica di (snapshot, cfg): chiavi ordinate,
    nessun timestamp. Base della cache content-addressed e dell'hash nel report."""
    payload = {
        "schema_version": snapshot.get("schema_version"),
        "snapshot": snapshot,
        "cfg": cfg.as_dict(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot_key(snapshot: dict, cfg: BidConfig) -> str:
    """Sha256 (hex) della rappresentazione canonica — cache key (§11)."""
    return hashlib.sha256(canonical_json(snapshot, cfg).encode("utf-8")).hexdigest()


# ------------------------------------------------------------- helper numerici
def _pct(sorted_list: list[int], p: float) -> int:
    """Percentile non interpolato (nearest-rank), deterministico."""
    if not sorted_list:
        return 0
    k = round(p * (len(sorted_list) - 1))
    return sorted_list[k]


def _r1(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


def _r3(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- report
def report(
    result: SimResult,
    snapshot: dict,
    cfg: BidConfig,
    *,
    deterministic_report: bool = False,
) -> dict:
    """Report JSON schema v1 (§8). Ordinamento canonico ovunque (pid/team);
    numeri rounded deterministicamente; ``deterministic_report=True`` => nessun
    timestamp (lo aggiunge la CLI solo quando serve)."""
    r = result
    n = r.n_runs
    if n <= 0:
        raise ValueError("n_runs deve essere >= 1 per generare un report")

    teams = snapshot["teams"]
    players = snapshot["players"]
    pool0 = snapshot.get("pool")
    if pool0 is None:
        pool0 = [p["pid"] for p in players]
    pid_nome = {p["pid"]: p["nome"] for p in players}
    pid_band = {p["pid"]: band_of(p["base"], tuple(cfg.band_edges)) for p in players}
    band0_counts: dict[int, int] = {}
    for p in players:
        b = pid_band[p["pid"]]
        band0_counts[b] = band0_counts.get(b, 0) + 1

    # ---- per_player -----------------------------------------------------
    per_player: list[dict] = []
    for pl in sorted(players, key=lambda p: p["pid"]):
        pid = pl["pid"]
        n_sold = r.n_sold.get(pid, 0)
        n_unsold = n - n_sold
        sale_prob = n_sold / n
        avg_price = (r.sum_price.get(pid, 0) / n_sold) if n_sold else None
        n_round = r.n_round.get(pid, 0)
        avg_comp = (r.sum_competition.get(pid, 0.0) / n_round) if n_round else 0.0

        entry: dict[str, Any] = {
            "pid": pid,
            "nome": pl["nome"],
            "band": pid_band[pid],
            "n_sold": n_sold,
            "n_unsold": n_unsold,
            "n_passed": r.n_passed.get(pid, 0),
            "sale_prob": _r3(sale_prob),
            "avg_price": _r1(avg_price),
        }
        if r.price_list is not None:
            sold = r.price_list.get(pid)
            if sold:
                s = sorted(sold)
                entry["p10_price"] = _pct(s, 0.10)
                entry["p50_price"] = _pct(s, 0.50)
                entry["p90_price"] = _pct(s, 0.90)
        entry["avg_competition"] = _r1(avg_comp)

        entry["market"] = {
            "base": pl["base"],
            "pma": _r1(pl.get("pma")),
            "suggested_snapshot": _r1(pl.get("suggested0")),
            "scarcity_snapshot": _r3(pl.get("scarc0")),
            "sim_avg_price": _r1(avg_price),
        }
        entry["model_value"] = {
            "value": pl.get("value"),
            "rank": pl.get("rank"),
            "source": "input",
        }
        bm = pl.get("base")
        pm = pl.get("pma")
        mv = pl.get("value")
        entry["divergence"] = {
            "sim_minus_base": _r1(avg_price - bm)
            if (avg_price is not None and bm is not None)
            else None,
            "sim_minus_pma": _r1(avg_price - pm)
            if (avg_price is not None and pm is not None)
            else None,
            "sim_minus_value": _r1(avg_price - mv)
            if (avg_price is not None and mv is not None)
            else None,
        }
        per_player.append(entry)

    # ---- per_team -------------------------------------------------------
    per_team: list[dict] = []
    for t in sorted(teams, key=lambda x: x["team"]):
        team = t["team"]
        top_rosters = []
        for combo, freq in r.combos[team].most_common(cfg.top_k):
            top_rosters.append(
                {
                    "players": sorted(pid_nome[pid] for pid in combo),
                    "freq": freq,
                    "pct": _r3(freq / n),
                }
            )
        per_team.append(
            {
                "team": team,
                "budget0": t["budget"],
                "slots_a0": t["slots_a"],
                "budget_end_avg": _r1(r.sum_budget_end[team] / n),
                "slots_filled_avg": _r1(r.sum_slots_filled[team] / n),
                "unfilled_prob": _r3(r.unfilled_count[team] / n),
                "top_rosters": top_rosters,
            }
        )

    # ---- team_player_prob ----------------------------------------------
    team_player_prob: list[dict] = []
    for t in sorted(teams, key=lambda x: x["team"]):
        team = t["team"]
        counts = r.team_player_counts[team]
        for pid in sorted(counts):
            team_player_prob.append(
                {"team": team, "pid": pid, "prob": _r3(counts[pid] / n)}
            )

    # ---- competition ------------------------------------------------------
    total_rounds = r.total_rounds
    completion_phase = {
        "n_runs_with_phase": r.completion_runs,
        "n_runs_without": n - r.completion_runs,
        "avg_forced_purchases": _r1(r.completion_purchases / r.completion_runs)
        if r.completion_runs
        else 0.0,
    }
    per_band = [
        {
            "band": b,
            "avg_competition": _r1(r.band_comp[b] / r.band_rounds[b])
            if r.band_rounds.get(b)
            else 0.0,
            "n_players": band0_counts.get(b, 0),
        }
        for b in sorted(band0_counts)
    ]
    competition = {
        "avg_eligible_overall": _r1(r.total_competition / total_rounds)
        if total_rounds
        else 0.0,
        "share_single_bidder": _r3(r.n_rounds_1bid / total_rounds)
        if total_rounds
        else 0.0,
        "share_two_plus": _r3(r.n_rounds_2plus / total_rounds) if total_rounds else 0.0,
        "share_zero_bids": _r3(r.n_rounds_0bid / total_rounds) if total_rounds else 0.0,
        "completion_phase": completion_phase,
        "per_band": per_band,
    }

    feasibility = dict(r.feasibility)
    feasibility["warnings"] = list(feasibility.get("warnings", [])) + list(r.warnings)

    league = {
        "money_league": sum(tt["budget"] for tt in teams),
        "n_teams": len(teams),
        "n_players": len(players),
        "n_pool0": len(pool0),
    }

    out: dict[str, Any] = {
        "schema_version": 1,
        "seed": cfg.seed,
        "cfg": cfg.as_dict(),
        "snapshot_hash": snapshot_key(snapshot, cfg),
        "feasibility": feasibility,
        "league": league,
        "per_player": per_player,
        "per_team": per_team,
        "team_player_prob": team_player_prob,
        "competition": competition,
        "warnings": list(r.warnings),
    }
    return out


def write_json_report(
    path: str, payload: dict, *, deterministic_report: bool = False
) -> None:
    """Scrive il report su file (JSON indentato). Se ``deterministic_report`` e'
    falso aggiunge il timestamp ``generated_at`` (unico campo non deterministico)."""
    from datetime import datetime, timezone

    data = dict(payload)
    if not deterministic_report:
        data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except OSError as e:
        raise RuntimeError(f"impossibile scrivere il report in {path}: {e}") from e
