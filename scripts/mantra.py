"""Fantacalcio Mantra roles, official formations, and tactical coverage helpers.

The auction roster is intentionally role-free (only its total size and two
keepers are constrained).  Fine-grained roles are used by the calculator to
measure formation coverage, scarcity, alternatives, and multi-role value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

MANTRA_ROLE_ORDER = ("Por", "Dd", "Ds", "Dc", "B", "E", "M", "C", "T", "W", "A", "Pc")
MANTRA_ROLE_LABEL = {
    "Por": "Portiere",
    "Dd": "Terzino destro",
    "Ds": "Terzino sinistro",
    "Dc": "Difensore centrale",
    "B": "Braccetto",
    "E": "Esterno",
    "M": "Mediano",
    "C": "Centrocampista",
    "T": "Trequartista",
    "W": "Ala",
    "A": "Attaccante di raccordo",
    "Pc": "Punta centrale",
}
_ROLE_CANON = {role.lower(): role for role in MANTRA_ROLE_ORDER}
_ROLE_CANON.update({"p": "Por", "por": "Por"})


def _slot(*roles: str) -> frozenset[str]:
    return frozenset(roles)


# The eleven fixed Mantra systems. A set in one position means alternatives,
# not multiple players. The third position in a back three accepts Dc/B.
MANTRA_FORMATIONS: dict[str, tuple[frozenset[str], ...]] = {
    "3-4-3": (
        _slot("Por"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Dc", "B"),
        _slot("E"),
        _slot("M", "C"),
        _slot("C"),
        _slot("E"),
        _slot("W", "A"),
        _slot("A", "Pc"),
        _slot("W", "A"),
    ),
    "3-4-1-2": (
        _slot("Por"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Dc", "B"),
        _slot("E"),
        _slot("M", "C"),
        _slot("C"),
        _slot("E"),
        _slot("T"),
        _slot("A", "Pc"),
        _slot("A", "Pc"),
    ),
    "3-4-2-1": (
        _slot("Por"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Dc", "B"),
        _slot("E", "W"),
        _slot("M", "C"),
        _slot("C"),
        _slot("E"),
        _slot("T"),
        _slot("T", "A"),
        _slot("A", "Pc"),
    ),
    "3-5-2": (
        _slot("Por"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Dc", "B"),
        _slot("E", "W"),
        _slot("M", "C"),
        _slot("M"),
        _slot("C"),
        _slot("E"),
        _slot("A", "Pc"),
        _slot("A", "Pc"),
    ),
    "3-5-1-1": (
        _slot("Por"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Dc", "B"),
        _slot("E", "W"),
        _slot("M"),
        _slot("C"),
        _slot("M"),
        _slot("E", "W"),
        _slot("T", "A"),
        _slot("A", "Pc"),
    ),
    "4-3-3": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("M", "C"),
        _slot("M"),
        _slot("C"),
        _slot("W", "A"),
        _slot("A", "Pc"),
        _slot("W", "A"),
    ),
    "4-3-1-2": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("M", "C"),
        _slot("M"),
        _slot("C"),
        _slot("T"),
        _slot("T", "A", "Pc"),
        _slot("A", "Pc"),
    ),
    "4-4-2": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("E", "W"),
        _slot("M", "C"),
        _slot("C"),
        _slot("E"),
        _slot("A", "Pc"),
        _slot("A", "Pc"),
    ),
    "4-1-4-1": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("M"),
        _slot("E", "W"),
        _slot("C", "T"),
        _slot("T"),
        _slot("W"),
        _slot("A", "Pc"),
    ),
    "4-4-1-1": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("E", "W"),
        _slot("M"),
        _slot("C"),
        _slot("E", "W"),
        _slot("T", "A"),
        _slot("A", "Pc"),
    ),
    "4-2-3-1": (
        _slot("Por"),
        _slot("Dd"),
        _slot("Dc"),
        _slot("Dc"),
        _slot("Ds"),
        _slot("M"),
        _slot("M", "C"),
        _slot("W", "T"),
        _slot("T"),
        _slot("W", "A"),
        _slot("A", "Pc"),
    ),
}
DEFAULT_MANTRA_FORMATION = "4-3-3"


def parse_roles(value: Any, classic_role: str | None = None) -> tuple[str, ...]:
    """Parse ``Dc;B``/``W/A``/iterables into canonical, de-duplicated roles.

    A classic-role fallback keeps legacy CSV files usable, but precise Mantra
    calculations are available as soon as ``ruolo_mantra`` is present.
    """
    raw: list[Any]
    if isinstance(value, str):
        raw = re.split(r"[;,/|\s]+", value.strip()) if value.strip() else []
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    for item in raw:
        role = _ROLE_CANON.get(str(item).strip().lower())
        if role and role not in out:
            out.append(role)
    if not out:
        fallback = {"P": "Por", "D": "Dc", "C": "C", "A": "A"}.get(
            str(classic_role or "").strip().upper()
        )
        if fallback:
            out.append(fallback)
    return tuple(out)


def has_explicit_roles(player: Mapping[str, Any]) -> bool:
    if "mantra_roles_explicit" in player:
        return bool(player.get("mantra_roles_explicit"))
    raw = (
        player.get("ruoli_mantra")
        or player.get("ruolo_mantra")
        or player.get("roleMantra")
    )
    return bool(parse_roles(raw))


def player_roles(player: Mapping[str, Any]) -> tuple[str, ...]:
    cached = player.get("ruoli_mantra")
    if (
        isinstance(cached, (list, tuple))
        and cached
        and all(isinstance(item, str) and item in MANTRA_ROLE_ORDER for item in cached)
    ):
        return tuple(cached)
    return parse_roles(
        player.get("ruoli_mantra")
        or player.get("ruolo_mantra")
        or player.get("roleMantra"),
        str(player.get("ruolo") or ""),
    )


def roles_text(player: Mapping[str, Any]) -> str:
    return "/".join(player_roles(player))


def formation_slots(name: str) -> tuple[frozenset[str], ...]:
    return MANTRA_FORMATIONS.get(name, MANTRA_FORMATIONS[DEFAULT_MANTRA_FORMATION])


_POS_CACHE: dict[tuple[tuple[str, ...], str], tuple[int, ...]] = {}


def compatible_positions(player: Mapping[str, Any], formation: str) -> tuple[int, ...]:
    roles = player_roles(player)
    key = (roles, formation)
    cached = _POS_CACHE.get(key)
    if cached is not None:
        return cached
    role_set = set(roles)
    positions = tuple(
        i
        for i, accepted in enumerate(formation_slots(formation))
        if role_set & accepted
    )
    # Distinct role combos are few (~26): caching makes market scarcity scans cheap.
    if len(_POS_CACHE) < 512:
        _POS_CACHE[key] = positions
    return positions


def same_job(a: Mapping[str, Any], b: Mapping[str, Any], formation: str) -> bool:
    return bool(
        set(compatible_positions(a, formation))
        & set(compatible_positions(b, formation))
    )


def _nonnegative_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0.0, number)


def best_lineup(
    players: Sequence[Mapping[str, Any]],
    formation: str,
    utility: Callable[[Mapping[str, Any]], float] | None = None,
) -> dict[str, Any]:
    """Return the maximum-cardinality, maximum-utility legal Mantra XI.

    Dynamic programming enforces both rules that matter for the auction:
    every player is used once and every tactical position is filled once.
    Cardinality is prioritised over utility, so a versatile player is assigned
    where the whole squad benefits most.
    """
    slots = formation_slots(formation)
    util = utility or (lambda _p: 1.0)
    # mask -> (utility sum, [(player_index, position_index), ...])
    dp: dict[int, tuple[float, tuple[tuple[int, int], ...]]] = {0: (0.0, ())}
    for player_index, player in enumerate(players):
        positions = compatible_positions(player, formation)
        if not positions:
            continue
        nxt = dict(dp)
        value = _nonnegative_number(util(player))
        for mask, (score, assignments) in dp.items():
            for pos in positions:
                bit = 1 << pos
                if mask & bit:
                    continue
                candidate = (score + value, assignments + ((player_index, pos),))
                previous = nxt.get(mask | bit)
                if previous is None or candidate[0] > previous[0]:
                    nxt[mask | bit] = candidate
        dp = nxt
    best_mask, (score, assignments) = max(
        dp.items(), key=lambda item: (item[0].bit_count(), item[1][0])
    )
    unfilled = [
        "/".join(role for role in MANTRA_ROLE_ORDER if role in accepted)
        for i, accepted in enumerate(slots)
        if not best_mask & (1 << i)
    ]
    return {
        "filled": best_mask.bit_count(),
        "score": score,
        "assignments": assignments,
        "unfilled": unfilled,
        "complete": best_mask.bit_count() == len(slots),
    }


def roster_players(auction: Any, team: str) -> list[Mapping[str, Any]]:
    return [
        auction.players[pid]
        for pid, _price, owner, _role in auction.state["sold"]
        if owner == team
    ]


def roster_spots_left(auction: Any, team: str) -> int:
    if team not in auction.state["money"]:
        return 0
    size = _nonnegative_number(auction.cfg.get("roster_size"))
    return max(0, round(size) - len(roster_players(auction, team)))


def tactical_impact(
    auction: Any,
    player: Mapping[str, Any],
    team: str,
    utility: Callable[[Mapping[str, Any]], float],
) -> dict[str, Any]:
    formation = auction.cfg.get("mantra_formation", DEFAULT_MANTRA_FORMATION)
    roster = roster_players(auction, team)
    before = best_lineup(roster, formation, utility)
    after = best_lineup([*roster, player], formation, utility)
    fills_hole = after["filled"] > before["filled"]
    score_gain = max(0.0, after["score"] - before["score"])
    player_utility = max(_nonnegative_number(utility(player)), 1e-9)
    gain = 1.0 if fills_hole else min(1.0, score_gain / player_utility)
    return {
        "formation": formation,
        "roles": list(player_roles(player)),
        "versatility": len(compatible_positions(player, formation)),
        "filled_before": before["filled"],
        "filled_after": after["filled"],
        "holes_before": before["unfilled"],
        "holes_after": after["unfilled"],
        "fills_hole": fills_hole,
        "marginal_gain": gain,
        "roster_spots_left": roster_spots_left(auction, team),
    }
