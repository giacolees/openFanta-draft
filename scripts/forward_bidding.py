#!/usr/bin/env -S uv run --quiet python
"""Round d'asta e formule di prezzo del simulatore Attaccanti (WP isolato).

Contiene la configurazione dell'asta (``BidConfig``), le formule bounded del
prezzo atteso adattato, il max bid con riserva di completamento, la
willingness-to-pay campionata e lo svolgimento del singolo round
(``conduct_round`` -> ``RoundOutcome``).

Regole di separazione (obbligatorie per il contratto WP7):
- la formula di PREZZO di mercato usa SOLO input di mercato (base/pma/
  suggested0/scarc0/unc_pfc): ``expected_price``, ``max_bid``, ``opening_price``,
  ammontare della WTP. ``value``/``rank`` NON vi entrano mai.
- ``rank`` modula SOLO la probabilita' di pass (``interest_score``/``pass_prob``)
  in fase normale, con fallback neutro: rank assente -> pass_prob = 0.
- ``eligible`` e' STRUTTURALE (monotona decrescente nella run) e non dipende da
  value/rank; ``interested`` = `eligible` ∧ non ha passato (fase completion:
  nessun pass).

Puro Python stdlib (``random``, ``dataclasses``, ``math``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from forward_state import ForwardState, PlayerView, TeamView


# ---------------------------------------------------------------- configurazione
@dataclass(frozen=True)
class BidConfig:
    n_runs: int = 10_000
    seed: int = 42
    player_order: str = "shuffle"  # "shuffle" | "by_value" (base desc)
    floor_price: int = 2  # floor vendita E riserva (default = min_slot_price)
    role_floor_price: dict[str, int] = field(
        default_factory=dict
    )  # hook WP6, es. {"A": 2}
    aggression: float = 1.25  # >= 1.0 (cap max offerta)
    beta_comp: float = 0.5  # peso # squadre ancora interessate
    beta_budget: float = 0.3  # peso budget medi interessate
    beta_scarc: float = 0.5  # peso scarsita' comparabile (domanda/offerta fascia)
    ratio_min: float = 0.5  # clamp dei rapporti di adattamento
    ratio_max: float = 2.0
    mu_cap_factor: float = 1.25  # mu_max = mu_cap_factor * suggested0
    sigma_coef: float = 0.10  # deviazione default se manca unc_pfc
    sigma_floor: float = 1.0
    price_premium: int = 1  # incremento sopra il secondo bid (asta inglese)
    pass_base: float = 0.6  # probabilita' base di pass in fase normale
    pass_max: float = 0.35  # tetto della probabilità di pass
    band_edges: tuple[float, ...] = (0, 5, 10, 25, 50, 100, 200)
    collect_prices: bool = True  # percentili per giocatore (lista prezzi)
    top_k: int = 5

    # ------------------------------------------------------------- validazione
    def validate(self) -> list[str]:
        """Errori di configurazione (lista vuota = ok). Mai muta nulla."""
        errs: list[str] = []
        if self.n_runs < 1:
            errs.append("n_runs deve essere >= 1")
        if not isinstance(self.seed, int):
            errs.append("seed deve essere un intero")
        if self.player_order not in ("shuffle", "by_value"):
            errs.append("player_order deve essere 'shuffle' o 'by_value'")
        if self.floor_price < 1:
            errs.append("floor_price deve essere >= 1")
        if self.aggression < 1.0:
            errs.append("aggression deve essere >= 1.0")
        for key, v in (
            ("beta_comp", self.beta_comp),
            ("beta_budget", self.beta_budget),
            ("beta_scarc", self.beta_scarc),
        ):
            if not (0.0 < v <= 1.0):
                errs.append(f"{key} deve essere in (0, 1]")
        if self.ratio_min <= 0:
            errs.append("ratio_min deve essere > 0")
        if not (self.ratio_min < self.ratio_max):
            errs.append("ratio_min < ratio_max richiesto")
        if not (self.ratio_min <= 1.0 <= self.ratio_max):
            errs.append("ratio_min <= 1 <= ratio_max richiesto (clamp con identita')")
        if self.mu_cap_factor < 1.0:
            errs.append("mu_cap_factor deve essere >= 1.0")
        if not (0.0 <= self.sigma_coef <= 1.0):
            errs.append("sigma_coef deve essere in [0, 1]")
        if self.sigma_floor < 0:
            errs.append("sigma_floor deve essere >= 0")
        if self.price_premium < 0:
            errs.append("price_premium deve essere >= 0")
        if not (0.0 <= self.pass_base <= 1.0):
            errs.append("pass_base deve essere in [0, 1]")
        if not (0.0 <= self.pass_max <= 1.0):
            errs.append("pass_max deve essere in [0, 1]")
        be = list(self.band_edges)
        if any(be[i] >= be[i + 1] for i in range(len(be) - 1)):
            errs.append("band_edges deve essere strettamente crescente")
        if self.top_k < 1:
            errs.append("top_k deve essere >= 1")
        return errs

    # ------------------------------------------- rappresentazione canonica
    def as_dict(self) -> dict:
        """Dict ordinato della config (JSON canonico per cache/report)."""
        return {
            "n_runs": self.n_runs,
            "seed": self.seed,
            "player_order": self.player_order,
            "floor_price": self.floor_price,
            "role_floor_price": self.role_floor_price,
            "aggression": self.aggression,
            "beta_comp": self.beta_comp,
            "beta_budget": self.beta_budget,
            "beta_scarc": self.beta_scarc,
            "ratio_min": self.ratio_min,
            "ratio_max": self.ratio_max,
            "mu_cap_factor": self.mu_cap_factor,
            "sigma_coef": self.sigma_coef,
            "sigma_floor": self.sigma_floor,
            "price_premium": self.price_premium,
            "pass_base": self.pass_base,
            "pass_max": self.pass_max,
            "band_edges": list(self.band_edges),
            "collect_prices": self.collect_prices,
            "top_k": self.top_k,
        }


def _floor_for_role(role: str, cfg: BidConfig) -> int:
    return cfg.role_floor_price.get(role, cfg.floor_price)


# ------------------------------------------------------------- pavimenti/riserva
def min_price_for(p: PlayerView, cfg: BidConfig) -> int:
    """Pavimento di vendita e di riserva per il giocatore: per ruolo A
    ``max(1, min(floor_A, base))``. Per ``base >= floor`` vale ``floor``; per
    ``base == 1`` vale 1 (mai 0)."""
    floor = _floor_for_role("A", cfg)
    return max(1, min(floor, p.base))


def reserve_needed(t: TeamView, cfg: BidConfig) -> int:
    """Riserva di completamento = Σ_ruolo floor_ruolo × slot_aperti_ruolo.
    Slot A: ``slots_a``; slot altri: ``slots_other``. Default (slots_other
    vuoto): floor_A × slots_a."""
    total = _floor_for_role("A", cfg) * t.slots_a
    for r, n in t.slots_other.items():
        total += _floor_for_role(r, cfg) * n
    return total


def team_infeasible(t: TeamView, cfg: BidConfig) -> bool:
    """budget < riserva di completamento -> non puo' completare la rosa A."""
    return t.budget < reserve_needed(t, cfg)


# ------------------------------------------------- eleggibilità / interesse
def eligible(t: TeamView, p: PlayerView, state: ForwardState, cfg: BidConfig) -> bool:
    """Eleggibilita' STRUTTURALE (monotona decrescente nella run): pid nel pool,
    slot A > 0, budget >= min_price_for(p), squadra non infeasible. Non dipende
    da value/rank, che non entrano mai qui ne' nel prezzo."""
    return (
        p.pid in state.pool
        and t.slots_a > 0
        and t.budget >= min_price_for(p, cfg)
        and not team_infeasible(t, cfg)
    )


def interest_score(p: PlayerView, n_players: int, cfg: BidConfig) -> float:
    """1.0 se rank assente (fallback NEUTRO). Altrimenti
    clamp(0.5 + 0.5 * (1 - (rank - 1) / max(1, n_players)), 0.5, 1.0).
    rank piu' alto (numero basso) -> interesse piu' alto. Mai nella formula di
    prezzo: modula solo la probabilita' di pass."""
    if p.rank is None:
        return 1.0
    score = 0.5 + 0.5 * (1.0 - (p.rank - 1) / max(1, n_players))
    return max(0.5, min(1.0, score))


def pass_prob(p: PlayerView, n_players: int, cfg: BidConfig) -> float:
    """clamp(pass_base * (1 - interest_score(p)), 0.0, pass_max).
    Fallback neutro (rank assente): interest = 1.0 -> pass_prob = 0.0."""
    return max(
        0.0,
        min(cfg.pass_max, cfg.pass_base * (1.0 - interest_score(p, n_players, cfg))),
    )


def interested(
    t: TeamView,
    p: PlayerView,
    state: ForwardState,
    statics: Any,
    cfg: BidConfig,
    rng: random.Random,
    *,
    phase: str = "normal",
) -> bool:
    """Interesse CAMPIONATA: eligible ∧ (phase == "completion" ∨ random() >=
    pass_prob(p)). Con rank assente pass_prob = 0 -> interested ≡ eligible."""
    if not eligible(t, p, state, cfg):
        return False
    if phase == "completion":
        return True
    return rng.random() >= pass_prob(p, statics.n_players, cfg)


def opening_price(p: PlayerView, cfg: BidConfig) -> int:
    """Prezzo di apertura = min_price_for(p): il prezzo pagato dal bidder
    singolo e il pavimento di ogni vendita."""
    return min_price_for(p, cfg)


# ------------------------------------------------------------------ prezzo atteso
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def expected_price(
    p: PlayerView,
    state: ForwardState,
    statics: Any,
    cfg: BidConfig,
    team_budgets: dict[str, int] | None = None,
    band_rem: dict[int, int] | None = None,
    *,
    elig: list[TeamView] | None = None,
    reserves: dict[str, int] | None = None,
) -> float:
    """Aspettativa di prezzo di mercato adattata (§4.2), bounded in
    [min_p(p), max_p(p)]. Usa solo input di mercato/scarsità/budget: MAI
    value/rank. ``team_budgets`` (ridondante con state.teams) e' accettato per
    API-compat; ``band_rem`` e' il contatore incrementale O(1) di fascia per
    run (se assente conta dal pool, O(P), solo per test); ``elig``/``reserves``
    sono il path O(1) del driver (liste/viste precalcolate per run)."""
    pid = p.pid
    mu0 = statics.mu0[pid]
    min_p = statics.min_p[pid]
    max_p = statics.max_p[pid]
    n_comp0 = statics.n_comp0[pid]
    avg_budget0 = statics.avg_budget0[pid]
    band0 = statics.band_counts0.get(p.band, 0)

    if elig is None:
        elig = [t for t in state.teams if eligible(t, p, state, cfg)]
    n_comp = len(elig)
    if team_budgets:
        avg_budget = sum(team_budgets[t.team] for t in elig) / n_comp if n_comp else 0.0
    else:
        avg_budget = sum(t.budget for t in elig) / n_comp if n_comp else 0.0

    R_comp = (
        _clamp(n_comp / n_comp0, cfg.ratio_min, cfg.ratio_max) if n_comp0 > 0 else 1.0
    )

    if band_rem is not None:
        n_rem_band = band_rem.get(p.band, 0)
    else:
        n_rem_band = sum(
            1 for q in state.players if q.pid in state.pool and q.band == p.band
        )

    num = n_comp / n_rem_band if n_rem_band > 0 else 0.0
    den = n_comp0 / band0 if band0 > 0 else 0.0
    if den > 0:
        R_scarc = _clamp(num / den, cfg.ratio_min, cfg.ratio_max)
    else:
        R_scarc = 1.0

    if avg_budget0 > 0:
        R_budget = (
            _clamp(avg_budget / avg_budget0, cfg.ratio_min, cfg.ratio_max)
            if n_comp
            else cfg.ratio_min
        )
    else:
        R_budget = 1.0

    mu_raw = (
        mu0
        * (R_comp**cfg.beta_comp)
        * (R_scarc**cfg.beta_scarc)
        * (R_budget**cfg.beta_budget)
    )
    return _clamp(mu_raw, min_p, max_p)


# ------------------------------------------------------------------ max bid / WTP
def max_bid(
    t: TeamView,
    p: PlayerView,
    mu: float,
    cfg: BidConfig,
    *,
    reserves: dict[str, int] | None = None,
    min_p: int | None = None,
) -> int | None:
    """max bid con riserva di completamento:
    min(round(mu*aggression), budget - reserve_dopo, budget).
    ``None`` se il tetto scende sotto ``min_price_for(p)`` (nessuna offerta).
    ``reserves``/``min_p`` (path O(1) del driver) evitano i ricalcoli per run;
    assenti si ricalcolano esattamente da (t, p, cfg)."""
    if min_p is None:
        min_p = min_price_for(p, cfg)
    aggr_cap = round(mu * cfg.aggression)
    if reserves is not None:
        reserve_dopo = reserves[t.team] - _floor_for_role("A", cfg)
    else:
        after = TeamView(t.team, t.budget, t.slots_a - 1, dict(t.slots_other))
        reserve_dopo = reserve_needed(after, cfg)
    reserve_cap = t.budget - reserve_dopo
    cap = min(aggr_cap, reserve_cap, t.budget)
    if cap < min_p:
        return None
    return cap


def sample_wtp(rng: random.Random, mu: float, sigma: float, cfg: BidConfig) -> int:
    """WTP = round(mu + sigma * gauss(0,1)) — NON clampata qui (clamp a
    max_bid/min nel round). Deterministico dato l'rng."""
    return round(mu + sigma * rng.gauss(0, 1))


# ------------------------------------------------------------------ round d'asta
@dataclass(frozen=True)
class RoundOutcome:
    pid: str
    phase: str  # "normal" | "completion"
    bids: tuple[
        tuple[str, int], ...
    ]  # (team, bid) ordinate desc, canonical order su parità
    winner: str | None
    price: int | None  # None ⇔ nessuna offerta valida (no-bid)
    n_eligible: int  # per metrica di concorrenza
    n_passed: int  # elegibili che hanno passato (fase normal)
    avg_budget: float = 0.0  # budget medio delle squadre elegibili (uso interno)


def conduct_round(
    state: ForwardState,
    pid: str,
    statics: Any,
    cfg: BidConfig,
    rng: random.Random,
    *,
    phase: str = "normal",
    band_rem: dict[int, int] | None = None,
    reserves: dict[str, int] | None = None,
) -> RoundOutcome:
    """Un round = una nomina (O(T)): in fase "normal" ogni squadra eleggibile
    puo' passare con probabilita' pass_prob(p) (§4.5); in fase "completion"
    nessun pass (completion forcing §6). Aggiudica e ritorna l'esito (§4.4).
    Non muta lo stato. ``reserves`` e' il path O(1) del driver (riserve correnti
    per squadra): assente -> calcolo esatto da (state, cfg)."""
    p = statics.players[pid]
    min_p = statics.min_p[pid]
    if reserves is not None:
        elig = [
            t
            for t in state.teams
            if pid in state.pool
            and t.slots_a > 0
            and t.budget >= min_p
            and t.budget >= reserves[t.team]
        ]
    else:
        elig = [t for t in state.teams if eligible(t, p, state, cfg)]
    mu = expected_price(
        p, state, statics, cfg, None, band_rem, elig=elig, reserves=reserves
    )
    sigma = statics.sigma[pid]
    pp = statics.pass_prob[pid]

    bids: list[tuple[str, int]] = []
    n_eligible = 0
    n_passed = 0
    budget_acc = 0
    for t in elig:
        n_eligible += 1
        budget_acc += t.budget
        if phase == "normal" and rng.random() < pp:
            n_passed += 1
            continue
        mb = max_bid(t, p, mu, cfg, reserves=reserves, min_p=min_p)
        if mb is None:
            continue
        wtp = sample_wtp(rng, mu, sigma, cfg)
        bid = min(max(wtp, min_p), mb)
        bids.append((t.team, bid))

    avg_budget = budget_acc / n_eligible if n_eligible else 0.0

    winner: str | None = None
    price: int | None = None
    if bids:
        idx = statics.team_index
        bids_sorted = sorted(bids, key=lambda b: (-b[1], idx[b[0]]))
        winner, winner_bid = bids_sorted[0]
        if len(bids) >= 2:
            second_bid = bids_sorted[1][1]
            interim = max(min_p, second_bid + cfg.price_premium)
            price = min(interim, winner_bid)
        else:  # bidder singolo: paga il prezzo di apertura/pavimento (non la WTP)
            price = opening_price(p, cfg)
    else:
        bids_sorted = ()
    return RoundOutcome(
        pid=pid,
        phase=phase,
        bids=tuple(bids_sorted),
        winner=winner,
        price=price,
        n_eligible=n_eligible,
        n_passed=n_passed,
        avg_budget=avg_budget,
    )
