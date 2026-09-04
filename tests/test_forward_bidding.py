"""Test delle formule e del round d'asta (WP forward) — vedi §12.2.

Formule bounded (min_price/reserva/max_bid/expected_price), tie, no-bid,
prezzo bidder singolo, e separazione eligible/interested con pass (rank) e
completion forcing. Indipendente dalla suite degli altri WP.
"""

from __future__ import annotations

import math
import random

from openfanta.forward.bidding import (  # pyright: ignore[reportMissingImports]
    BidConfig,
    conduct_round,
    eligible,
    expected_price,
    interest_score,
    interested,
    max_bid,
    min_price_for,
    opening_price,
    pass_prob,
    reserve_needed,
    sample_wtp,
    team_infeasible,
)
from openfanta.forward.sim import (  # pyright: ignore[reportMissingImports]
    SimStatics,
    _build_statics,
)
from openfanta.forward.state import (  # pyright: ignore[reportMissingImports]
    TeamView,
    state_from_snapshot,
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
    pid: str,
    nome: str,
    base: int = 50,
    *,
    rank: int | None = None,
    unc_pfc: float | None = 5.0,
) -> dict:
    return {
        "pid": pid,
        "nome": nome,
        "base": base,
        "pma": _f(base) * 1.05,
        "unc_pfc": unc_pfc,
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


def _statics(snapshot: dict, cfg: BidConfig | None = None) -> tuple:
    cfg = cfg or CFG
    state = state_from_snapshot(snapshot, cfg)
    return state, _build_statics(state, cfg)


# ------------------------------------------------------------- min_price_for
def test_min_price_for():
    p1 = make_player("p1", "X", base=1)
    p2 = make_player("p2", "Y", base=5)
    p3 = make_player("p3", "Z", base=100)
    st = state_from_snapshot(make_snapshot(players=[p1, p2, p3]), CFG)

    def pv(pid):
        return next(q for q in st.players if q.pid == pid)

    assert min_price_for(pv("p1"), CFG) == 1  # base 1 -> mai 0
    assert min_price_for(pv("p2"), CFG) == 2  # base >= floor -> floor
    assert min_price_for(pv("p3"), CFG) == 2  # floor
    # override ruolo A
    cfg = BidConfig(role_floor_price={"A": 5})
    assert min_price_for(pv("p1"), cfg) == max(1, min(5, 1)) == 1
    assert min_price_for(pv("p2"), cfg) == 5
    assert min_price_for(pv("p3"), cfg) == 5
    assert opening_price(pv("p3"), CFG) == min_price_for(pv("p3"), CFG)


# ------------------------------------------------------------- riserva
def test_reserve_needed():
    t = TeamView("IO", 100, 3)
    assert reserve_needed(t, CFG) == 2 * 3
    t2 = TeamView("IO", 100, 3, {"P": 2, "D": 4})
    assert reserve_needed(t2, CFG) == 2 * 3 + 2 * 2 + 2 * 4
    cfg = BidConfig(role_floor_price={"A": 5, "P": 3, "D": 1})
    assert (
        reserve_needed(TeamView("IO", 100, 2, {"P": 2, "D": 4}), cfg)
        == 5 * 2 + 3 * 2 + 1 * 4
    )


def test_team_infeasible_boundary():
    cfg = BidConfig(floor_price=2)
    assert not team_infeasible(TeamView("IO", 6, 3), cfg)  # == reserve -> feasible
    assert team_infeasible(TeamView("IO", 5, 3), cfg)  # reserve - 1 -> infeasible


# ------------------------------------------------------------- max_bid
def test_max_bid_caps():
    _, statics = _statics(make_snapshot())
    pv = statics.players["p1"]
    # cap aggression attivo con riserva piccola
    t = TeamView("IO", 100, 2)
    mu = 100.0
    mb = max_bid(t, pv, mu, CFG)
    # min(round(100*1.25)=125, 100 - 2*1 = 98, 100) = 98
    assert mb == 98
    # cap riserva attivo con budget tirato
    t2 = TeamView("IO", 10, 3)
    mb2 = max_bid(t2, pv, mu, CFG)
    # round(125) vs 10 - 2*2 = 6 vs 10 -> 6
    assert mb2 == 6
    # squadra infeasible (budget < riserva): max_bid puo' essere None o >= min_p
    t3 = TeamView("IO", 5, 3)
    assert team_infeasible(t3, CFG)
    assert max_bid(t3, pv, mu, CFG) is None or max_bid(
        t3, pv, mu, CFG
    ) >= min_price_for(pv, CFG)
    # cap sotto il pavimento -> None
    t4 = TeamView("IO", 2, 3)  # budget 2, riserva 6 -> infeasible
    assert team_infeasible(t4, CFG)
    mb4 = max_bid(t4, pv, mu, CFG)
    assert mb4 is None
    # mai sotto min_p per squadre feasibili
    t5 = TeamView("IO", 60, 3)  # riserva 6, feasible
    mb5 = max_bid(t5, pv, 2.0, CFG)
    assert mb5 >= min_price_for(pv, CFG)


# ------------------------------------------------------------- expected_price
def _pv(pid, statics):
    return statics.players[pid]


def test_expected_price_ratios_one_gives_mu0():
    snap = make_snapshot()
    state, statics = _statics(snap)
    p = _pv("p1", statics)
    mu = expected_price(p, state, statics, CFG)
    assert math.isclose(mu, statics.mu0["p1"], abs_tol=1e-6)
    assert mu == min(max(mu, statics.min_p["p1"]), statics.max_p["p1"])


def test_expected_price_scales_with_halved_competition():
    # stato sintetico controllato: 4 squadre elegibili all'inizio; ne restano 2
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2), ("T2", 20, 2), ("T3", 20, 2)],
        players=[make_player("p1", "ALFA", 60)],
        pool=["p1"],
    )
    state, statics = _statics(snap)
    p = _pv("p1", statics)
    assert statics.n_comp0["p1"] == 4
    # hand-built statics per controllare band/scarcita' in isolamento
    cfg = BidConfig()
    st = SimStatics(
        players=statics.players,
        team_order=statics.team_order,
        team_index=statics.team_index,
        band=statics.band,
        band_counts0={statics.band["p1"]: 4},
        mu0={"p1": 100.0},
        sigma={"p1": 1.0},
        min_p={"p1": 2},
        max_p={"p1": 200},
        n_comp0={"p1": 4},
        avg_budget0={"p1": 100.0},
        pass_prob={"p1": 0.0},
        interest={"p1": 1.0},
        n_players=1,
        slots_a0={t.team: t.slots_a for t in state.teams},
    )
    # state con 4 teams uguali: ne rendo 2 ineleggibili (budget < min_p resta
    # comunque pool valido -> uso slots_a=0 su T2/T3)
    mutated = state_from_snapshot(
        make_snapshot(
            teams=[("IO", 100, 2), ("T1", 100, 2), ("T2", 20, 0), ("T3", 20, 0)],
            players=[make_player("p1", "ALFA", 60)],
            pool=["p1"],
        ),
        cfg,
    )
    mu = expected_price(p, mutated, st, cfg, None, {statics.band["p1"]: 2})
    # R_comp = 2/4 = 0.5; R_scarc = (2/2)/(4/4) = 1; R_budget = 100/100 = 1
    want = 100.0 * (0.5**cfg.beta_comp)
    assert math.isclose(mu, want, rel_tol=1e-6)


def test_expected_price_clamp_min_max():
    snap = make_snapshot(players=[make_player("p1", "ALFA", 60)])
    state, statics = _statics(snap)
    p = _pv("p1", statics)
    st = SimStatics(
        players=statics.players,
        team_order=statics.team_order,
        team_index=statics.team_index,
        band=statics.band,
        band_counts0=statics.band_counts0,
        mu0={"p1": 10.0},
        sigma={"p1": 1.0},
        min_p={"p1": 12},
        max_p={"p1": 200},
        n_comp0={"p1": 2},
        avg_budget0={"p1": 100.0},
        pass_prob={"p1": 0.0},
        interest={"p1": 1.0},
        n_players=1,
        slots_a0={t.team: t.slots_a for t in state.teams},
    )
    mu = expected_price(p, state, st, CFG)  # rapporti = 1 -> mu = 10 -> clamp a 12
    assert mu == 12
    st2 = SimStatics(
        players=statics.players,
        team_order=statics.team_order,
        team_index=statics.team_index,
        band=statics.band,
        band_counts0=statics.band_counts0,
        mu0={"p1": 300.0},
        sigma={"p1": 1.0},
        min_p={"p1": 2},
        max_p={"p1": 200},
        n_comp0={"p1": 2},
        avg_budget0={"p1": 100.0},
        pass_prob={"p1": 0.0},
        interest={"p1": 1.0},
        n_players=1,
        slots_a0={t.team: t.slots_a for t in state.teams},
    )
    assert expected_price(p, state, st2, CFG) == 200


def test_expected_price_finite_all_edges():
    snap = make_snapshot(
        players=[make_player("p1", "ALFA", 60), make_player("p2", "BETA", 2)]
    )
    state, statics = _statics(snap)
    for pid in ("p1", "p2"):
        mu = expected_price(statics.players[pid], state, statics, CFG)
        assert math.isfinite(mu)
        assert statics.min_p[pid] <= mu <= statics.max_p[pid]


# ------------------------------------------------------------- WTP / round
def test_sample_wtp_deterministic_and_round():
    rng = random.Random(7)
    a = [sample_wtp(rng, 100.0, 5.0, CFG) for _ in range(50)]
    rng2 = random.Random(7)
    b = [sample_wtp(rng2, 100.0, 5.0, CFG) for _ in range(50)]
    assert a == b
    assert all(type(x) is int for x in a)
    assert all(abs(x - 100) <= 40 for x in a)  # sigma 5 -> ~3 sigma


def tie_snapshot():
    # due squadre identiche (stesse riserve) con sigma=0 -> WTP identiche -> tie
    p = make_player("p1", "ALFA", base=50, unc_pfc=None)
    p = {**p, "suggested0": 50.0}
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2)],
        players=[p],
        pool=["p1"],
    )
    cfg = BidConfig(sigma_coef=0.0, sigma_floor=0.0)
    return snap, cfg


def test_conduct_round_tie_canonical():
    snap, cfg = tie_snapshot()
    state, statics = _statics(snap, cfg)
    rng = random.Random(1)
    o = conduct_round(
        state,
        "p1",
        statics,
        cfg,
        rng,
        phase="normal",
        band_rem=dict(statics.band_counts0),
    )
    assert o.winner == "IO"  # ordine canonico
    assert len(o.bids) == 2
    bids = {t for t, b in o.bids}
    assert bids == {"IO", "T1"}
    b_io = dict(o.bids)["IO"]
    assert o.price == b_io  # tie: price = min(second+premium, own) = own
    assert o.price >= min_price_for(statics.players["p1"], cfg)
    # il vincitore copre il prezzo
    io = next(t for t in state.teams if t.team == "IO")
    assert io.budget >= o.price


def test_conduct_round_single_bidder_pays_opening():
    p = make_player("p1", "ALFA", base=3, unc_pfc=None)
    p = {**p, "suggested0": 50.0}
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 0)],  # T1 senza slot -> un solo bidder
        players=[p],
        pool=["p1"],
    )
    cfg = BidConfig(sigma_coef=0.0, sigma_floor=0.0)
    state, statics = _statics(snap, cfg)
    rng = random.Random(1)
    o = conduct_round(
        state,
        "p1",
        statics,
        cfg,
        rng,
        phase="normal",
        band_rem=dict(statics.band_counts0),
    )
    assert o.winner == "IO"
    assert len(o.bids) == 1
    assert o.price == opening_price(statics.players["p1"], cfg)  # = min_p, NON la WTP
    assert o.price == 2  # base 3 -> min(2, 3) = 2
    bid_io = dict(o.bids)["IO"]
    assert o.price != bid_io  # il bidder singolo non paga la propria WTP


def test_conduct_round_second_price():
    p = make_player("p1", "ALFA", base=50, unc_pfc=None)
    p = {**p, "suggested0": 50.0}
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T2", 20, 2)],  # T2 budget basso -> cap riserva
        players=[p],
        pool=["p1"],
    )
    cfg = BidConfig(sigma_coef=0.0, sigma_floor=0.0)
    state, statics = _statics(snap, cfg)
    rng = random.Random(2)
    o = conduct_round(
        state,
        "p1",
        statics,
        cfg,
        rng,
        phase="normal",
        band_rem=dict(statics.band_counts0),
    )
    assert o.winner == "IO"
    assert len(o.bids) == 2
    b_map = dict(o.bids)
    b_io, b_t2 = b_map["IO"], b_map["T2"]
    assert b_io > b_t2
    assert o.price == b_t2 + 1  # secondo bid + premium
    assert o.price <= b_io  # mai sopra la propria offerta
    assert o.price >= min_price_for(statics.players["p1"], cfg)


def test_conduct_round_no_bids():
    snap = make_snapshot(
        teams=[("IO", 100, 0), ("T1", 100, 0)],  # nessuno con slot
        players=[make_player("p1", "ALFA", 60)],
        pool=["p1"],
    )
    state, statics = _statics(snap)
    rng = random.Random(3)
    o = conduct_round(state, "p1", statics, CFG, rng, phase="completion")
    assert o.winner is None
    assert o.price is None
    assert len(o.bids) == 0


def test_conduct_round_winner_budget_covers():
    snap, cfg = tie_snapshot()
    state, statics = _statics(snap, cfg)
    rng = random.Random(4)
    for team in state.teams:
        assert team.budget >= 0
    o = conduct_round(
        state,
        "p1",
        statics,
        cfg,
        rng,
        phase="normal",
        band_rem=dict(statics.band_counts0),
    )
    if o.winner:
        t = next(x for x in state.teams if x.team == o.winner)
        assert t.budget >= o.price


# ------------------------------------------------------------- interest / pass
def test_interest_and_pass_bounds():
    n_players = 8
    # rank assente: neutro
    p_none = make_player("p1", "X", rank=None)
    st0 = state_from_snapshot(make_snapshot(players=[p_none]), CFG)
    pv = st0.players[0]
    assert interest_score(pv, n_players, CFG) == 1.0
    assert pass_prob(pv, n_players, CFG) == 0.0
    # rank 1: interesse massimo, pass 0
    p1 = make_player("p1", "X", rank=1)
    st = state_from_snapshot(make_snapshot(players=[p1]), CFG)
    pv1 = st.players[0]
    assert interest_score(pv1, n_players, CFG) == 1.0
    assert pass_prob(pv1, n_players, CFG) == 0.0
    # rank peggiore: interesse minimo bounded, pass al tetto
    p8 = make_player("p1", "X", rank=8)
    st8 = state_from_snapshot(make_snapshot(players=[p8]), CFG)
    pv8 = st8.players[0]
    s8 = interest_score(pv8, n_players, CFG)
    assert 0.5 <= s8 <= 1.0
    pp8 = pass_prob(pv8, n_players, CFG)
    assert 0.0 <= pp8 <= CFG.pass_max
    assert pp8 > 0.0
    # monotona: rank peggiore -> pass piu' alta
    for r in range(2, 9):
        pl = make_player("p1", "X", rank=r)
        stt = state_from_snapshot(make_snapshot(players=[pl]), CFG)
        assert pass_prob(stt.players[0], n_players, CFG) >= 0.0
    assert pp8 > pass_prob(pv1, n_players, CFG)
    # mai nan
    assert not math.isnan(s8) and not math.isnan(pp8)


def test_eligible_is_structural():
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2), ("T2", 1, 2)],
        players=[make_player("p1", "ALFA", 60, rank=8)],
        pool=["p1"],
    )
    state, statics = _statics(snap)
    pv = statics.players["p1"]
    io = next(t for t in state.teams if t.team == "IO")
    t2 = next(t for t in state.teams if t.team == "T2")
    # T2 budget 1 < min_p 2 -> non eleggibile; value/rank non entrano in eligible
    assert eligible(io, pv, state, CFG)
    assert not eligible(t2, pv, state, CFG)


def test_interested_pass_statistics_and_completion_no_pass():
    # scenario: 1 giocatore, rank pessimo (pass alta), due squadre eleggibili
    snap = make_snapshot(
        teams=[("IO", 100, 2), ("T1", 100, 2)],
        players=[make_player("p1", "ALFA", 60, rank=8)],
        pool=["p1"],
    )
    state, statics = _statics(snap)
    pp = statics.pass_prob["p1"]
    assert pp > 0.0
    rng = random.Random(99)
    n_rounds = 3000
    passed = 0
    elig = 0
    for _ in range(n_rounds):
        o = conduct_round(
            state,
            "p1",
            statics,
            CFG,
            rng,
            phase="normal",
            band_rem=dict(statics.band_counts0),
        )
        elig += o.n_eligible
        passed += o.n_passed
    rate = passed / elig
    assert abs(rate - pp) < 0.05  # test statistico seeded
    # fase completion: nessun pass anche con rank presente
    rng2 = random.Random(99)
    passed_c = 0
    for _ in range(n_rounds):
        o = conduct_round(
            state,
            "p1",
            statics,
            CFG,
            rng2,
            phase="completion",
            band_rem=dict(statics.band_counts0),
        )
        assert o.n_passed == 0
        passed_c += o.n_passed
    assert passed_c == 0


def test_interested_helper():
    snap = make_snapshot(
        teams=[("IO", 100, 2)],
        players=[make_player("p1", "ALFA", 60, rank=1)],
        pool=["p1"],
    )
    state, statics = _statics(snap)
    pv = statics.players["p1"]
    io = state.teams[0]
    rng = random.Random(1)
    _ = pv  # pv resta in scope: niente unused
    assert interested(io, pv, state, statics, CFG, rng, phase="normal")
    assert interested(io, pv, state, statics, CFG, rng, phase="completion")
