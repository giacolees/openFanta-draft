#!/usr/bin/env -S uv run --quiet python
"""Driver Monte Carlo del simulatore Attaccanti (WP isolato).

Precompute delle ``SimStatics`` (una volta per snapshot), esecuzione deterministica
di ``n_runs`` aste complete della fase A (fase normale con un round per giocatore
e pass opzionali, poi fase di completamento di completion-forcing §6) e raccolta
degli accumulatori raw in ``SimResult`` (le probabilita' le calcola
``forward_agg``).

Determinismo: un unico ``random.Random(cfg.seed)`` consumato in sequenza per
TUTTE le run; stesso (seed, snapshot, cfg) => stessi numeri.
Terminazione: round totali <= 2·P per run; la fase di completamento parte solo
se serve e si ferma su un pass completo senza vendite (nessuna squadra eleggibile).
Invariante di fine run: ``state.check() == []``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from openfanta.forward.bidding import (
    BidConfig,
    conduct_round,
    eligible,
    interest_score,
    min_price_for,
    pass_prob,
    reserve_needed,
    team_infeasible,
)
from openfanta.forward.state import (
    ForwardState,
    PlayerView,
    TeamView,
    purchase,
    state_from_snapshot,
    validate_snapshot,
)


# ------------------------------------------------------------ statics
@dataclass(frozen=True)
class SimStatics:
    """Derivate UNA volta dallo snapshot+cfg (O(P log P)): band per giocatore,
    mu0, sigma, min/max price, band_counts0, n_comp0(p), avg_budget0(p),
    interest_score(p), ordine squadre canonico. Niente calcoli per run."""

    players: dict[str, PlayerView]  # pid -> PlayerView
    team_order: tuple[str, ...]
    team_index: dict[str, int]  # team -> indice canonico (tie-break)
    band: dict[str, int]  # pid -> fascia
    band_counts0: dict[int, int]  # fascia -> conteggio iniziale
    mu0: dict[str, float]
    sigma: dict[str, float]
    min_p: dict[str, int]
    max_p: dict[str, int]
    n_comp0: dict[str, int]
    avg_budget0: dict[str, float]
    pass_prob: dict[str, float]
    interest: dict[str, float]
    n_players: int
    slots_a0: dict[str, int]  # team -> slot A iniziali


@dataclass(frozen=True)
class RunSummary:  # solo con trace=True (test)
    purchases: tuple[
        tuple[int, str, str, str, int], ...
    ]  # (round_no, phase, pid, team, price)
    end_pool: frozenset[str]
    end_teams: tuple[TeamView, ...]
    unfilled: tuple[str, ...]  # squadre con slots_a > 0 a fine run


@dataclass
class SimResult:
    """Accumulatori raw (niente probabilita' qui — le calcola forward_agg)."""

    n_runs: int
    n_sold: dict[str, int]
    n_passed: dict[str, int]
    n_round: dict[str, int]
    sum_price: dict[str, int]
    sum_price2: dict[str, int]
    sum_competition: dict[str, float]
    price_list: dict[str, list[int]] | None  # se cfg.collect_prices
    sum_avg_budget: dict[str, float]
    combos: dict[str, Counter[frozenset[str]]]
    unfilled_count: dict[str, int]
    sum_budget_end: dict[str, int]
    sum_slots_filled: dict[str, int]
    team_player_counts: dict[str, Counter[str]]
    shortfall_per_run: list[int]
    completion_runs: int
    completion_purchases: int
    total_rounds: int
    total_competition: float
    n_rounds_0bid: int
    n_rounds_1bid: int
    n_rounds_2plus: int
    band_rounds: dict[int, int]
    band_comp: dict[int, float]
    warnings: list[str]
    feasibility: dict


# ------------------------------------------------------------ precompute statics
def _build_statics(state: ForwardState, cfg: BidConfig) -> SimStatics:
    n_players = len(state.players)
    players = {p.pid: p for p in state.players}
    team_order = tuple(t.team for t in state.teams)
    team_index = {team: i for i, team in enumerate(team_order)}
    band = {p.pid: p.band for p in state.players}
    band_counts0: dict[int, int] = {}
    for p in state.players:
        band_counts0[p.band] = band_counts0.get(p.band, 0) + 1

    mu0 = {p.pid: p.suggested0 for p in state.players}
    sigma: dict[str, float] = {}
    for p in state.players:
        var = p.unc_pfc
        if var is not None and var > 0:
            try:
                s = float(var)
            except (TypeError, ValueError):
                s = cfg.sigma_coef * p.suggested0
        else:
            s = cfg.sigma_coef * p.suggested0
        sigma[p.pid] = max(s, cfg.sigma_floor)
    min_p = {p.pid: min_price_for(p, cfg) for p in state.players}
    max_p = {p.pid: round(cfg.mu_cap_factor * p.suggested0) for p in state.players}

    n_comp0: dict[str, int] = {}
    avg_budget0: dict[str, float] = {}
    for p in state.players:
        elig = [t for t in state.teams if eligible(t, p, state, cfg)]
        n_comp0[p.pid] = len(elig)
        avg_budget0[p.pid] = (sum(t.budget for t in elig) / len(elig)) if elig else 0.0

    interest = {p.pid: interest_score(p, n_players, cfg) for p in state.players}
    passp = {p.pid: pass_prob(p, n_players, cfg) for p in state.players}
    slots_a0 = {t.team: t.slots_a for t in state.teams}
    return SimStatics(
        players=players,
        team_order=team_order,
        team_index=team_index,
        band=band,
        band_counts0=band_counts0,
        mu0=mu0,
        sigma=sigma,
        min_p=min_p,
        max_p=max_p,
        n_comp0=n_comp0,
        avg_budget0=avg_budget0,
        pass_prob=passp,
        interest=interest,
        n_players=n_players,
        slots_a0=slots_a0,
    )


# ------------------------------------------------------------ static feasibility
def _static_feasibility(state: ForwardState, snapshot: dict, cfg: BidConfig) -> dict:
    infeas: list[dict] = []
    for t in state.teams:
        if team_infeasible(t, cfg):
            infeas.append(
                {
                    "team": t.team,
                    "budget": t.budget,
                    "slots_a": t.slots_a,
                    "reserve_needed": reserve_needed(t, cfg),
                }
            )
    feasible = [t for t in state.teams if not team_infeasible(t, cfg)]
    total_slots = sum(t.slots_a for t in feasible)
    shortfall = max(0, total_slots - len(state.pool))

    warnings: list[str] = []
    spent_unknown = snapshot.get("spent_unknown", 0)
    if spent_unknown:
        warnings.append(
            f"ALTRO speso nel motore ({spent_unknown} cr) escluso dalla simulazione"
        )
    missing_rank = sum(1 for p in state.players if p.rank is None or p.value is None)
    if missing_rank:
        warnings.append(f"value/rank assenti per {missing_rank} giocatori")
    if not state.pool:
        warnings.append("pool vuoto")

    teams_infeasible = infeas
    ok = shortfall == 0 and not teams_infeasible
    return {
        "ok": ok,
        "shortfall": shortfall,
        "teams_infeasible": teams_infeasible,
        "warnings": warnings,
    }


# ------------------------------------------------------------ run accumulators
def _accumulate_outcome(res: SimResult, o: Any, statics: SimStatics) -> None:
    pid = o.pid
    res.n_round[pid] = res.n_round.get(pid, 0) + 1
    res.sum_competition[pid] = res.sum_competition.get(pid, 0.0) + o.n_eligible
    res.n_passed[pid] = res.n_passed.get(pid, 0) + o.n_passed
    res.sum_avg_budget[pid] = res.sum_avg_budget.get(pid, 0.0) + o.avg_budget
    res.total_rounds += 1
    res.total_competition += o.n_eligible
    b = statics.band[pid]
    res.band_rounds[b] = res.band_rounds.get(b, 0) + 1
    res.band_comp[b] = res.band_comp.get(b, 0.0) + o.n_eligible
    nb = len(o.bids)
    if nb == 0:
        res.n_rounds_0bid += 1
    elif nb == 1:
        res.n_rounds_1bid += 1
    else:
        res.n_rounds_2plus += 1
    if o.winner is not None:
        res.n_sold[pid] = res.n_sold.get(pid, 0) + 1
        res.sum_price[pid] = res.sum_price.get(pid, 0) + o.price
        res.sum_price2[pid] = res.sum_price2.get(pid, 0) + o.price * o.price
        if res.price_list is not None:
            res.price_list.setdefault(pid, []).append(o.price)


def _needs_completion(state: ForwardState, statics: SimStatics, cfg: BidConfig) -> bool:
    if not state.pool:
        return False
    return any(t.slots_a > 0 and not team_infeasible(t, cfg) for t in state.teams)


def _base_desc(pid: str, statics: SimStatics) -> int:
    return statics.players[pid].base


def _apply_sale(
    state: ForwardState,
    pid: str,
    team: str,
    price: int | None,
    cfg: BidConfig,
    statics: SimStatics,
    band_rem: dict[int, int],
    reserves: dict[str, int],
    floor_a: int,
    bought: dict[str, set[str]],
    purchases: list[tuple[int, str, str, str, int]],
    round_no: int,
    phase: str,
) -> ForwardState:
    """Applica una vendita (fase normal o completion): transizione immutabile +
    contatori incrementali per run. Prezzo garantito dal round (winner ⇒ price)."""
    if price is None:
        raise RuntimeError(f"round {round_no}: winner senza prezzo ({phase})")
    new_state = purchase(state, pid, team, price, cfg)
    band_rem[statics.band[pid]] -= 1
    reserves[team] -= floor_a
    bought[team].add(pid)
    purchases.append((round_no, phase, pid, team, price))
    return new_state


# ------------------------------------------------------------ simulate
def simulate(
    snapshot: dict, cfg: BidConfig, *, trace: bool = False
) -> SimResult | tuple[SimResult, list[RunSummary]]:
    """Monte Carlo deterministico (§5). rng = random.Random(cfg.seed) usato in
    sequenza per TUTTE le run. Ritorna ``SimResult``; se ``trace=True`` ritorna
    ``(SimResult, [RunSummary, ...])`` con un summary per run."""
    import random

    errs = validate_snapshot(snapshot, cfg)
    if errs:
        raise ValueError("snapshot invalido: " + "; ".join(errs))
    errs = cfg.validate()
    if errs:
        raise ValueError("config invalida: " + "; ".join(errs))

    state0 = state_from_snapshot(snapshot, cfg)
    statics = _build_statics(state0, cfg)
    feasibility = _static_feasibility(state0, snapshot, cfg)
    warnings = list(feasibility["warnings"])

    res = SimResult(
        n_runs=cfg.n_runs,
        n_sold={},
        n_passed={},
        n_round={},
        sum_price={},
        sum_price2={},
        sum_competition={},
        price_list={} if cfg.collect_prices else None,
        sum_avg_budget={},
        combos={t.team: Counter() for t in state0.teams},
        unfilled_count={t.team: 0 for t in state0.teams},
        sum_budget_end={t.team: 0 for t in state0.teams},
        sum_slots_filled={t.team: 0 for t in state0.teams},
        team_player_counts={t.team: Counter() for t in state0.teams},
        shortfall_per_run=[],
        completion_runs=0,
        completion_purchases=0,
        total_rounds=0,
        total_competition=0.0,
        n_rounds_0bid=0,
        n_rounds_1bid=0,
        n_rounds_2plus=0,
        band_rounds={},
        band_comp={},
        warnings=warnings,
        feasibility=feasibility,
    )

    rng = random.Random(cfg.seed)
    traces: list[RunSummary] | None = [] if trace else None
    feasible_teams = {t.team for t in state0.teams if not team_infeasible(t, cfg)}
    # riserve correnti per squadra (path O(1)): dipendono solo da slots_a
    # corrente + slots_other fissi -> inizializzate una volta, copiate per run
    # e decrementate di floor_A a ogni acquisto della squadra.
    floor_a = cfg.role_floor_price.get("A", cfg.floor_price)
    reserves0 = {t.team: reserve_needed(t, cfg) for t in state0.teams}

    for _ in range(cfg.n_runs):
        state = state0
        band_rem = dict(statics.band_counts0)
        reserves = dict(reserves0)
        pool_pids = sorted(state.pool)
        if cfg.player_order == "by_value":
            order = sorted(pool_pids, key=lambda pid: -_base_desc(pid, statics))
        else:
            order = list(pool_pids)
            rng.shuffle(order)

        bought: dict[str, set[str]] = defaultdict(set)
        purchases: list[tuple[int, str, str, str, int]] = []
        round_no = 0
        completion_used = False
        completion_buys = 0

        # fase normale: un round per giocatore
        for pid in order:
            if pid not in state.pool:
                continue
            round_no += 1
            outcome = conduct_round(
                state,
                pid,
                statics,
                cfg,
                rng,
                phase="normal",
                band_rem=band_rem,
                reserves=reserves,
            )
            _accumulate_outcome(res, outcome, statics)
            if outcome.winner:
                state = _apply_sale(
                    state,
                    pid,
                    outcome.winner,
                    outcome.price,
                    cfg,
                    statics,
                    band_rem,
                    reserves,
                    floor_a,
                    bought,
                    purchases,
                    round_no,
                    "normal",
                )

        # fase di completamento (completion forcing §6)
        if _needs_completion(state, statics, cfg):
            completion_used = True
            while _needs_completion(state, statics, cfg):
                progressed = False
                for pid in order:
                    if pid not in state.pool:
                        continue
                    round_no += 1
                    outcome = conduct_round(
                        state,
                        pid,
                        statics,
                        cfg,
                        rng,
                        phase="completion",
                        band_rem=band_rem,
                        reserves=reserves,
                    )
                    _accumulate_outcome(res, outcome, statics)
                    if outcome.winner:
                        state = _apply_sale(
                            state,
                            pid,
                            outcome.winner,
                            outcome.price,
                            cfg,
                            statics,
                            band_rem,
                            reserves,
                            floor_a,
                            bought,
                            purchases,
                            round_no,
                            "completion",
                        )
                        completion_buys += 1
                        progressed = True
                if not progressed:
                    break

        if completion_used:
            res.completion_runs += 1
        res.completion_purchases += completion_buys

        # finalizzazione accumulatori per-run (per squadra)
        end_by_team = {t.team: t for t in state.teams}
        for t in state0.teams:
            b = bought[t.team]
            end = end_by_team[t.team]
            res.sum_budget_end[t.team] += end.budget
            res.sum_slots_filled[t.team] += statics.slots_a0[t.team] - end.slots_a
            res.team_player_counts[t.team].update(b)
            if end.slots_a == 0:
                res.combos[t.team][frozenset(b)] += 1
            else:
                res.unfilled_count[t.team] += 1

        shortfall_r = sum(
            1 for t in state.teams if t.slots_a > 0 and t.team in feasible_teams
        )
        res.shortfall_per_run.append(shortfall_r)

        if traces is not None:
            unfilled = tuple(t.team for t in state.teams if t.slots_a > 0)
            traces.append(
                RunSummary(
                    purchases=tuple(purchases),
                    end_pool=state.pool,
                    end_teams=state.teams,
                    unfilled=unfilled,
                )
            )

    if trace:
        if traces is None:
            raise RuntimeError("trace richiesto ma lista non inizializzata")
        return res, traces
    return res
