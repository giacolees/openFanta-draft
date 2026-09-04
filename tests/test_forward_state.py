"""Test dello stato immutabile del simulatore (WP forward) — vedi §12.1.

Transizioni, immutabilita', invarianti e adapter ``snapshot_from_auction``.
Indipendente dalla suite degli altri WP: costruisce i propri Auction di test con
giocatori sintetici (schema WP3) e non dipende da conftest.
"""

from __future__ import annotations

import math

import pytest  # pyright: ignore[reportMissingImports]

from openfanta.forward.bidding import BidConfig  # pyright: ignore[reportMissingImports]
from openfanta.forward.state import (  # pyright: ignore[reportMissingImports]
    ForwardState,
    InsufficientBudgetError,
    InvalidPurchaseError,
    NotInPoolError,
    PriceBelowFloorError,
    SlotUnavailableError,
    SnapshotError,
    TeamNotFoundError,
    TeamView,
    band_of,
    purchase,
    snapshot_from_auction,
    state_from_snapshot,
    validate_snapshot,
)

CFG = BidConfig()


# ------------------------------------------------------------- helper locali
def _f(x):
    """float() difensivo per dati test (input iper-controllati)."""
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
    """Giocatore A nello schema snapshot v1."""
    return {
        "pid": pid,
        "nome": nome,
        "base": base,
        "pma": _f(base) * 1.05,
        "unc_pfc": 5.0,
        "suggested0": _f(base) * 1.1,
        "scarc0": 1.0,
        "unsold0": 0,
        "value": value,
        "rank": rank,
    }


def make_snapshot(
    teams: list[tuple[str, int, int]] | None = None,
    players: list[dict] | None = None,
    pool: list[str] | None = None,
) -> dict:
    """Snapshot sintetico minimo: teams (nome, budget, slots_a)."""
    teams = teams or [("IO", 100, 2), ("T1", 100, 2)]
    players = players or [
        make_player("p1", "ALFA", 60),
        make_player("p2", "BETA", 40),
        make_player("p3", "GAMMA", 20),
    ]
    return {
        "schema_version": 1,
        "teams": [
            {
                "team": t[0],
                "budget": t[1],
                "slots_a": t[2],
                "slots_other": {},
            }
            for t in teams
        ],
        "players": players,
        "pool": pool if pool is not None else [p["pid"] for p in players],
        "money_league": sum(t[1] for t in teams),
    }


def make_state(snapshot: dict | None = None) -> ForwardState:
    return state_from_snapshot(snapshot or make_snapshot(), CFG)


# ------------------------------------------------------------- purchase
def test_purchase_updates_budget_slot_pool():
    st = make_state()
    before = st
    st2 = purchase(st, "p1", "IO", 10, CFG)
    io = next(t for t in st2.teams if t.team == "IO")
    assert io.budget == 90
    assert io.slots_a == 1
    assert "p1" not in st2.pool
    assert st2.spent == 10
    # lo stato originale resta identico (immutabilita')
    assert st is before
    assert st.pool == frozenset({"p1", "p2", "p3"})
    assert next(t for t in st.teams if t.team == "IO").budget == 100


def test_purchase_immutability_structural():
    st = make_state()
    st2 = purchase(st, "p1", "IO", 10, CFG)
    assert st.teams is not st2.teams
    assert st.pool is not st2.pool
    assert st.players is st2.players  # tuple giocatori condivisa (mai mutata)
    # nessun alias tra le viste squadra
    assert st2.teams[0] is not st.teams[0]
    assert st.teams[0].budget == 100


def test_purchase_errors():
    st = make_state()
    with pytest.raises(NotInPoolError):
        purchase(st, "pX", "IO", 10, CFG)  # pid non nel pool
    st2 = purchase(st, "p1", "IO", 10, CFG)
    with pytest.raises(NotInPoolError):
        purchase(st2, "p1", "IO", 10, CFG)  # gia' venduto
    with pytest.raises(PriceBelowFloorError):
        purchase(st, "p3", "IO", 1, CFG)  # base 20 -> floor 2, prezzo 1
    with pytest.raises(InsufficientBudgetError):
        purchase(st, "p1", "IO", 2000, CFG)
    # slot esaurito
    st3 = purchase(st, "p1", "IO", 2, CFG)
    st3 = purchase(st3, "p2", "IO", 2, CFG)
    with pytest.raises(SlotUnavailableError):
        purchase(st3, "p3", "IO", 2, CFG)
    with pytest.raises(TeamNotFoundError):
        purchase(st, "p1", "T4", 2, CFG)
    with pytest.raises(InvalidPurchaseError):
        purchase(st, "p1", "IO", 0, CFG)  # errore generico: prezzo sotto il pavimento


def test_purchase_min_price_boundary():
    st = make_state()
    # prezzo == pavimento va bene, prezzo - 1 no
    st2 = purchase(st, "p2", "IO", 2, CFG)
    assert st2 is not None
    with pytest.raises(PriceBelowFloorError):
        purchase(st, "p2", "IO", 1, CFG)


# ------------------------------------------------------------- invarianti
def test_check_ok_after_arbitrary_valid_sequences():
    st = make_state()
    assert st.check() == []
    # sequenza arbitraria valida di acquisti (pid diversi, squadre diverse)
    st = purchase(st, "p1", "IO", 12, CFG)
    st = purchase(st, "p2", "T1", 8, CFG)
    st = purchase(st, "p3", "T1", 2, CFG)
    assert st.check() == []


def test_ledger_conserved():
    st = make_state()
    total0 = st.money_league
    prices = [12, 8, 5]
    for pid, price in zip(("p1", "p2", "p3"), prices, strict=False):
        team = "IO" if pid != "p2" else "T1"
        st = purchase(st, pid, team, price, CFG)
        assert sum(t.budget for t in st.teams) + st.spent == total0
        assert st.money_league == total0
    assert st.check() == []


def test_check_detects_corruption():
    st = make_state()
    broken = ForwardState(
        teams=(TeamView("IO", -1, 1),),
        players=(),
        pool=frozenset(),
        money_league=100,
    )
    errs = broken.check()
    assert any("budget" in e for e in errs)
    # pool con pid sconosciuto
    bad_pool = ForwardState(
        teams=(TeamView("IO", 100, 2),),
        players=st.players,
        pool=frozenset({"nope"}),
        money_league=100,
    )
    assert any("pool" in e for e in bad_pool.check())
    # ledger rotto
    bad_ledger = ForwardState(
        teams=(TeamView("IO", 90, 2),),
        players=st.players,
        pool=frozenset({"p1"}),
        money_league=100,
    )
    assert any("ledger" in e for e in bad_ledger.check())


# ------------------------------------------------------------- band / snapshot
def test_band_of():
    edges = (0, 5, 10, 25, 50, 100, 200)
    assert band_of(1, edges) == 0
    assert band_of(5, edges) == 1
    assert band_of(8, edges) == 1
    assert band_of(15, edges) == 2
    assert band_of(60, edges) == 4
    assert band_of(130, edges) == 5
    assert band_of(383, edges) == 6


def test_validate_snapshot_errors():
    good = make_snapshot()
    assert validate_snapshot(good, CFG) == []
    bad = dict(good)
    bad["schema_version"] = 2
    assert validate_snapshot(bad, CFG)
    bad2 = dict(good)
    bad2["teams"] = [{"team": "IO"}]  # budget/slots_a mancanti
    assert any("teams[0]" in e for e in validate_snapshot(bad2, CFG))
    bad3 = dict(good)
    bad3["players"] = [{"pid": "p1"}]  # base mancante
    assert any("players[0]" in e for e in validate_snapshot(bad3, CFG))
    bad4 = dict(good)
    bad4["pool"] = "notalist"
    assert any("pool" in e for e in validate_snapshot(bad4, CFG))
    with pytest.raises(SnapshotError):
        raise SnapshotError()


def test_state_from_snapshot_canonical():
    snap = make_snapshot()
    st = state_from_snapshot(snap, CFG)
    # player ordinati per pid (ordine canonico)
    pids = [p.pid for p in st.players]
    assert pids == sorted(pids)
    assert st.money_league == 200
    assert st.check() == []
    # round(pma): il ledger usa il budget, i float restano float
    assert all(math.isfinite(p.pma) for p in st.players)
    assert st.pool == frozenset(["p1", "p2", "p3"])


# ------------------------------------------------------------- snapshot_from_auction
def _engine_module():
    """Motore live (read-only). Se il motore non e' importabile (WP6 di altri
    agenti in corso, es. ``valuation`` non ancora creato) i test engine vengono
    saltati, non falliti: la suite forward resta indipendente dal WIP altrui."""
    try:
        from openfanta.core.auction import (
            Auction,  # pyright: ignore[reportMissingImports]
        )
    except ImportError as e:
        pytest.skip(f"motore live non importabile (WIP altri WP): {e}")
    return Auction


def _engine_fixture():
    Auction = _engine_module()

    players = [
        {
            "nome": "ALFA",
            "squadra": "",
            "ruolo": "A",
            "pfc": 60.0,
            "pma": 62.0,
            "pfc_range": "",
            "pma_range": "",
            "dpfcpma": 0.0,
            "slot": 2,
            "tit": 60.0,
            "expfm": 6.5,
            "fascia": "Top",
            "status": "T",
            "pen_prob": 0.0,
            "fk_prob": 0.0,
            "tix": 50.0,
            "fix": 50.0,
            "fix_contrib": 0.3,
        },
        {
            "nome": "BETA",
            "squadra": "",
            "ruolo": "A",
            "pfc": 40.0,
            "pma": 41.0,
            "pfc_range": "",
            "pma_range": "",
            "dpfcpma": 0.0,
            "slot": 2,
            "tit": 50.0,
            "expfm": 6.2,
            "fascia": "Good",
            "status": "T",
            "pen_prob": 0.0,
            "fk_prob": 0.0,
            "tix": 50.0,
            "fix": 50.0,
            "fix_contrib": 0.2,
        },
        {
            "nome": "GAMMA",
            "squadra": "",
            "ruolo": "A",
            "pfc": 20.0,
            "pma": 20.0,
            "pfc_range": "",
            "pma_range": "",
            "dpfcpma": 0.0,
            "slot": 3,
            "tit": 40.0,
            "expfm": 6.0,
            "fascia": "",
            "status": "B",
            "pen_prob": 0.0,
            "fk_prob": 0.0,
            "tix": 40.0,
            "fix": 40.0,
            "fix_contrib": 0.0,
        },
    ]
    return Auction(
        players,
        teams=2,
        budget=100,
        slots={"P": 1, "D": 1, "C": 1, "A": 4},
        formation={"P": 1, "D": 1, "C": 1, "A": 4},
    )


def test_snapshot_from_auction_real_engine():
    auction = _engine_fixture()
    alfa = auction.find("ALFA")
    auction.mark_sold(alfa, 55, "IO")
    gamma = auction.find("GAMMA")
    auction.mark_unsold(gamma)
    snap = snapshot_from_auction(auction)
    assert snap["schema_version"] == 1
    assert [t["team"] for t in snap["teams"]] == ["IO", "T1"]
    io = snap["teams"][0]
    assert io["budget"] == 45
    assert io["slots_a"] == 3
    assert io["slots_other"] == {"P": 1, "D": 1, "C": 1}
    pool_pids = set(snap["pool"])
    assert "ALFA" not in pool_pids
    gamma_row = next(p for p in snap["players"] if p["nome"] == "GAMMA")
    assert gamma_row["unsold0"] == 1
    # suggested0/scarc0 coerenti con auction.evaluate (una chiamata per lettura)
    beta = auction.find("BETA")
    ev = auction.evaluate(beta)
    beta_row = next(p for p in snap["players"] if p["nome"] == "BETA")
    assert beta_row["suggested0"] == round(_f(ev["suggested"]), 2)
    assert beta_row["scarc0"] == round(_f(ev["scarc"]), 4)


def test_snapshot_from_auction_values_and_teams_filter():
    auction = _engine_fixture()
    snap = snapshot_from_auction(
        auction,
        teams=["IO"],
        values={
            "aliv": {"value": 70.0, "rank": 1},
            "both": {"value": None, "rank": None},
        },
    )
    assert [t["team"] for t in snap["teams"]] == ["IO"]
    # pit value/rank assenti nello snapshot ma presenti in values: non inventati
    for p in snap["players"]:
        assert p["value"] is None and p["rank"] is None
