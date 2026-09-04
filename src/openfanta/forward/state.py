#!/usr/bin/env -S uv run --quiet python
"""Stato immutabile del simulatore della fase finale dell'asta Attaccanti (WP isolato).

Questo modulo e' il contenitore dello stato piu' interno del simulatore Monte
Carlo: vista squadra (``TeamView``), vista giocatore (``PlayerView``) e stato
complessivo (``ForwardState``) con le sole transizioni via ``purchase()``
(immutabile: ritorna una copia strutturale, non muta mai l'input).

Contiene inoltre:
- ``band_of`` / ``band_edges`` : calcolo della fascia prezzo di un giocatore.
- ``validate_snapshot`` / ``state_from_snapshot`` : lettura e validazione dello
  snapshot JSON v1 (schema con ``schema_version``, ``teams``, ``players``,
  ``pool``, ``money_league``).
- ``snapshot_from_auction`` : adapter READ-ONLY verso il motore live
  (``live_auction.Auction``) via duck-typing — nessun import circolare, nessuna
  modifica al motore. Il pid e' usato come identita'; se il motore esponesse un
  helper per la fonte di verita' non lo si duplica, lo si riusa (read-only).

Il modulo e' puro Python stdlib (``dataclasses``, ``math``). Le transizioni
(Gli errori di acquisto) sono indicate dalle eccezioni dedicate sotto, coerenti
con la semantica ``InvalidPurchaseError``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Floor di vendita/reserva per ruolo (dominio WP6/league_config, ri-esposto qui
# come costante di dominio). Il default reale e' in ``BidConfig`` (2); questo e'
# solo il pavimento assoluto (mai 0).
_MIN_PRICE = 1


# ------------------------------------------------------------------- eccezioni
class InvalidPurchaseError(Exception):
    """Violazione delle regole di acquisto nella transizione ``purchase``."""


class NotInPoolError(InvalidPurchaseError):
    """Il giocatore non e' (piu') nel pool."""


class PriceBelowFloorError(InvalidPurchaseError):
    """Il prezzo e' sotto il pavimento di vendita del giocatore."""


class InsufficientBudgetError(InvalidPurchaseError):
    """La squadra non ha crediti sufficienti."""


class SlotUnavailableError(InvalidPurchaseError):
    """La squadra ha gia' esaurito gli slot A."""


class TeamNotFoundError(InvalidPurchaseError):
    """La squadra non e' tracciata nello stato."""


class SnapshotError(ValueError):
    """Snapshot JSON v1 invalido (chiavi mancanti/tipi errati)."""


# ----------------------------------------------------------------- identita'
def _team_index_errors(team: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(team, dict):
        return ["voce team non e' un dict"]
    if "team" not in team or not isinstance(team["team"], str):
        errs.append("team: chiave 'team' mancante o non stringa")
    if "budget" not in team or type(team["budget"]) is not int or team["budget"] < 0:
        errs.append("team: chiave 'budget' mancante o non intero >= 0")
    if "slots_a" not in team or type(team["slots_a"]) is not int or team["slots_a"] < 0:
        errs.append("team: chiave 'slots_a' mancante o non intero >= 0")
    slots_other = team.get("slots_other")
    if slots_other is not None and not isinstance(slots_other, dict):
        errs.append("team: 'slots_other' non e' un dict")
    elif isinstance(slots_other, dict):
        for r, n in slots_other.items():
            if type(n) is not int or n < 0:
                errs.append(f"team.slots_other[{r!r}]: non intero >= 0")
    return errs


def _player_index_errors(player: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(player, dict):
        return ["voce player non e' un dict"]
    for key in ("pid", "nome"):
        if key not in player or not isinstance(player[key], str):
            errs.append(f"player: chiave '{key}' mancante o non stringa")
    for key in ("base", "suggested0", "scarc0", "unsold0"):
        if key not in player:
            errs.append(f"player: chiave '{key}' mancante")
    base = player.get("base")
    if type(base) is not int or base < 1:
        errs.append("player: 'base' non intero >= 1")
    for key in ("pma", "suggested0", "scarc0"):
        v = player.get(key)
        if not (isinstance(v, (int, float)) and math.isfinite(v)):
            errs.append(f"player: '{key}' non numerico finito")
    unsold0 = player.get("unsold0")
    if type(unsold0) is not int or unsold0 < 0:
        errs.append("player: 'unsold0' non intero >= 0")
    for key in ("unc_pfc", "value", "rank"):
        v = player.get(key)
        if v is not None and not isinstance(v, (int, float)):
            errs.append(f"player: '{key}' non numerico o None")
    return errs


def validate_snapshot(snapshot: Any, cfg: Any) -> list[str]:
    """Validazione dello snapshot JSON v1. Ritorna la lista errori (vuota = ok).

    Non muta nulla. ``cfg`` serve solo per leggere ``band_edges`` coerenti con
    la configurazione, ma la validazione e' indipendente dai valori di config.
    """
    errs: list[str] = []
    if not isinstance(snapshot, dict):
        return ["lo snapshot non e' un dict"]
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        errs.append(
            f"schema_version mancante o non {SCHEMA_VERSION} "
            f"(trovato {snapshot.get('schema_version')!r})"
        )
    teams = snapshot.get("teams")
    if not isinstance(teams, list) or not teams:
        errs.append("teams: lista non vuota attesa")
    else:
        for i, t in enumerate(teams):
            for e in _team_index_errors(t):
                errs.append(f"teams[{i}]: {e}")
    players = snapshot.get("players")
    if not isinstance(players, list) or not players:
        errs.append("players: lista non vuota attesa")
    else:
        for i, p in enumerate(players):
            for e in _player_index_errors(p):
                errs.append(f"players[{i}]: {e}")
    pool = snapshot.get("pool")
    if pool is None and isinstance(players, list):
        pass  # assente -> pool = tutti i pid (default)
    elif not isinstance(pool, list) or not all(isinstance(x, str) for x in pool):
        errs.append("pool: lista di stringhe attesa")
    return errs


def _band_edges(cfg: Any) -> tuple[float, ...]:
    """Edges di fascia dalla config (default se assenti)."""
    be = getattr(cfg, "band_edges", None)
    return tuple(be) if be else (0, 5, 10, 25, 50, 100, 200)


def band_of(base: int, band_edges: tuple[float, ...]) -> int:
    """Indice della fascia prezzo contenente ``base`` (band_edges crescenti).

    L'indice e' l'ultimo ``i`` tale che ``band_edges[i] <= base``; se ``base``
    precede la prima soglia vale 0. Deterministico.
    """
    idx = 0
    for i, edge in enumerate(band_edges):
        if base >= edge:
            idx = i
        else:
            break
    return idx


# ------------------------------------------------------------------ viste
@dataclass(frozen=True)
class TeamView:
    team: str
    budget: int  # crediti residui (int, >= 0)
    slots_a: int  # slot A residui (int, >= 0)
    slots_other: dict[str, int] = field(
        default_factory=dict
    )  # opzionale, solo per riserva


@dataclass(frozen=True)
class PlayerView:
    pid: str
    nome: str
    base: int  # PFC arrotondato (>= 1)
    pma: float
    unc_pfc: float | None  # semiampiezza range PFC (varianza WP3)
    suggested0: float  # prezzo live engine allo snapshot (>= min_price)
    scarc0: float  # scarcity engine allo snapshot (>= 0)
    unsold0: int  # invenduti reali allo snapshot (>= 0)
    band: int  # indice fascia prezzo (precalcolato)
    value: float | None = None  # modello valore (INPUT: mai nella formula di prezzo)
    rank: int | None = None  # ranking (INPUT: mai nel prezzo; modula il pass §4.5)


@dataclass(frozen=True)
class ForwardState:
    teams: tuple[TeamView, ...]
    players: tuple[PlayerView, ...]  # solo ruolo A, ordine canonico (pid)
    pool: frozenset[str]  # pid ancora in asta
    money_league: int  # == somma budget iniziali (lega chiusa, no ALTRO)
    # Spesa cumulata in-astica (semplifica l'invariante ledger: per una lega
    # chiusa ``sum(budget) + spent == money_league`` e' un fatto contabile
    # verificabile). Aggiunta interna a ForwardState (non nei campi "pubblici"
    # della spec) per rendere l'invariante n.3 di ``check()`` realmente testabile.
    spent: int = 0

    # ------------------------------------------------------------- invarianti
    def check(self) -> list[str]:
        """Invarianti di stato (lista vuota = ok).

        1. budget >= 0 per ogni squadra; slots_a >= 0; slot altri >= 0.
        2. pool ⊆ players; nessun pid duplicato in players.
        3. ledger: sum(budget) + spent == money_league (lega chiusa, no ALTRO).
        4. ogni valore numerico finito (math.isfinite).
        """
        problems: list[str] = []
        if not math.isfinite(self.money_league) or self.money_league < 0:
            problems.append(f"money_league non finito o negativo: {self.money_league}")
        if self.spent < 0:
            problems.append(f"spent negativo: {self.spent}")

        budget_sum = 0
        for t in self.teams:
            if not math.isfinite(t.budget) or t.budget < 0:
                problems.append(
                    f"team {t.team}: budget non finito o negativo ({t.budget})"
                )
            if t.slots_a < 0:
                problems.append(f"team {t.team}: slots_a negativo ({t.slots_a})")
            for r, n in t.slots_other.items():
                if not math.isfinite(n) or n < 0:
                    problems.append(
                        f"team {t.team}: slots_other[{r!r}] non finito o negativo ({n})"
                    )
            budget_sum += t.budget

        pids = [p.pid for p in self.players]
        if len(pids) != len(set(pids)):
            problems.append("players: pid duplicati")
        unknown = self.pool - set(pids)
        if unknown:
            problems.append(f"pool: pid non tracciati in players: {sorted(unknown)}")

        if budget_sum + self.spent != self.money_league:
            problems.append(
                f"ledger: sum(budget) {budget_sum} + spent {self.spent} != "
                f"money_league {self.money_league}"
            )

        for p in self.players:
            for key in ("base", "pma", "suggested0", "scarc0"):
                v = getattr(p, key)
                if not math.isfinite(v):
                    problems.append(f"player {p.pid}: {key} non finito ({v})")
            for key in ("unc_pfc", "value"):
                v = getattr(p, key)
                if v is not None and not math.isfinite(v):
                    problems.append(f"player {p.pid}: {key} non finito ({v})")
            if p.rank is not None and not math.isfinite(p.rank):
                problems.append(f"player {p.pid}: rank non finito ({p.rank})")
        return problems


# --------------------------------------------------------------- costruzione
def _coerce_slots_other(raw: Any) -> dict[str, int]:
    if not raw:
        return {}
    return {str(r): int(n) for r, n in raw.items()}


def _player_lookup(d: dict[str, Any]) -> PlayerView:
    return PlayerView(
        pid=d["pid"],
        nome=d["nome"],
        base=d["base"],
        pma=float(d["pma"]),
        unc_pfc=d.get("unc_pfc"),
        suggested0=float(d["suggested0"]),
        scarc0=float(d["scarc0"]),
        unsold0=int(d["unsold0"]),
        band=band_of(int(d["base"]), _band_edges(None)),
        value=d.get("value"),
        rank=d.get("rank"),
    )


def team_view_from_dict(d: dict[str, Any]) -> TeamView:
    return TeamView(
        team=d["team"],
        budget=int(d["budget"]),
        slots_a=int(d["slots_a"]),
        slots_other=_coerce_slots_other(d.get("slots_other")),
    )


def state_from_snapshot(snapshot: dict[str, Any], cfg: Any) -> ForwardState:
    """Costruisce il ``ForwardState`` iniziale da uno snapshot JSON v1.

    Le squadre e i player vengono canonizzati (ordine dello snapshot per le
    squadre; i player ordinati per pid). ``money_league`` e' derivato come
    somma dei budget iniziali (lega chiusa), non letto dallo snapshot, per
    garantire l'invariante ledger fin dallo stato iniziale.
    """
    teams = tuple(team_view_from_dict(t) for t in snapshot["teams"])
    players = tuple(
        sorted((_player_lookup(p) for p in snapshot["players"]), key=lambda p: p.pid)
    )
    if snapshot.get("pool") is not None:
        pool = frozenset(str(x) for x in snapshot["pool"])
    else:
        pool = frozenset(p.pid for p in players)
    money_league = sum(t.budget for t in teams)
    return ForwardState(
        teams=teams, players=players, pool=pool, money_league=money_league
    )


# ---------------------------------------------------------- transizioni (sola purchase)
def _find_team(state: ForwardState, team: str) -> TeamView | None:
    for t in state.teams:
        if t.team == team:
            return t
    return None


def _find_player(state: ForwardState, pid: str) -> PlayerView | None:
    for p in state.players:
        if p.pid == pid:
            return p
    return None


def purchase(
    state: ForwardState, pid: str, team: str, price: int, cfg: Any
) -> ForwardState:
    """Transizione IMMUTABILE: ritorna un nuovo ``ForwardState`` (copia
    strutturale O(T)). Alza ``InvalidPurchaseError`` se:
    - ``pid`` non nel pool (``NotInPoolError``);
    - ``price < min_price_for(p, cfg)`` (``PriceBelowFloorError``);
    - ``price > budget`` della squadra (``InsufficientBudgetError``);
    - ``slots_a == 0`` della squadra (``SlotUnavailableError``);
    - squadra non tracciata (``TeamNotFoundError``).
    Non muta mai lo stato dato.
    """
    from openfanta.forward.bidding import min_price_for  # evita l'import circolare

    if pid not in state.pool:
        raise NotInPoolError(f"giocatore {pid!r} non e' nel pool")
    p = _find_player(state, pid)
    if p is None:
        raise NotInPoolError(f"giocatore {pid!r} non tracciato in players")
    t = _find_team(state, team)
    if t is None:
        raise TeamNotFoundError(f"squadra {team!r} non tracciata")
    if price < min_price_for(p, cfg):
        raise PriceBelowFloorError(
            f"prezzo {price} sotto il pavimento {min_price_for(p, cfg)} per {p.nome}"
        )
    if price > t.budget:
        raise InsufficientBudgetError(
            f"{t.team} ha {t.budget} cr, il prezzo e' {price} cr"
        )
    if t.slots_a <= 0:
        raise SlotUnavailableError(f"{t.team} ha esaurito gli slot A")

    new_team = TeamView(t.team, t.budget - price, t.slots_a - 1, dict(t.slots_other))
    new_teams = tuple(new_team if x is t else x for x in state.teams)
    new_pool = state.pool - {pid}
    return ForwardState(
        teams=new_teams,
        players=state.players,
        pool=new_pool,
        money_league=state.money_league,
        spent=state.spent + price,
    )


# ------------------------------------------------------------- adapter motore
def snapshot_from_auction(
    auction: Any,
    *,
    teams: list[str] | None = None,
    values: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Snapshot JSON v1 dal motore live (duck-typed, READ-ONLY).

    Legge ``auction.state``/``auction.players``/``auction.evaluate`` senza
    importarlo (nessun ciclo di import e nessuna modifica al motore). Considera
    solo il ruolo ``A`` e le squadre in ``auction.state['money']`` (o quelle in
    ``teams`` se dato, che viene filtrato su quelle tracciate).

    Per ogni giocatore nel pool A chiama ``auction.evaluate(p)`` una sola volta
    (O(P) totale) per ricavare ``suggested``/``scarcity``; ``unsold0`` da
    ``auction.state['unsold']``. ``values``: mapping pid -> {"value": float|None,
    "rank": int|None} (INPUT separato, mai dentro la formula di prezzo).

    Il pid resta la fonte di verita' dell'identita': se il motore fornisce un
    helper di creazione della vista non lo si duplica — qui si legge
    direttamente dal dict giocatore gia' canonico del motore.
    """
    state = auction.state
    money = state["money"]
    tracked = list(money.keys())
    if teams is not None:
        allowed = {t for t in teams}
        tracked = [t for t in tracked if t in allowed]

    team_rows: list[dict[str, Any]] = []
    for name in tracked:
        slots = state["slots"][name]
        team_rows.append(
            {
                "team": name,
                "budget": int(money[name]),
                "slots_a": int(slots.get("A", 0)),
                "slots_other": {r: int(n) for r, n in slots.items() if r != "A"},
            }
        )

    pool_a: list[str] = []
    for pid in state["pool"]:
        pl = auction.players[pid]
        if pl.get("ruolo") == "A":
            pool_a.append(pid)

    player_rows: list[dict[str, Any]] = []
    for pid in sorted(pool_a):
        pl = auction.players[pid]
        ev = auction.evaluate(pl)
        sugg = ev.get("suggested", max(1, round(pl["base"])))
        scarc = ev.get("scarc", 0.0)
        base = int(pl.get("base", max(1, round(pl.get("pfc", 1)))))
        v = None
        r = None
        if values and pid in values:
            v = values[pid].get("value")
            r = values[pid].get("rank")
        player_rows.append(
            {
                "pid": pid,
                "nome": pl.get("nome", pid),
                "base": base,
                "pma": float(pl.get("pma", base)),
                "unc_pfc": pl.get("unc_pfc"),
                "suggested0": round(float(sugg), 2),
                "scarc0": round(float(scarc), 4),
                "unsold0": int(state.get("unsold", {}).get(pid, 0)),
                "value": v,
                "rank": r,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "teams": team_rows,
        "players": player_rows,
        "pool": sorted(pool_a),
        "money_league": int(
            state.get("money_league", sum(t["budget"] for t in team_rows))
        ),
    }
