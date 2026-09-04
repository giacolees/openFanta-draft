"""Acceptance tests for the Fantacalcio Mantra auction modality."""

import league_config  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import make_player  # pyright: ignore[reportMissingImports]
from live_auction import (  # pyright: ignore[reportMissingImports]
    Auction,
    SlotUnavailableError,
)
from mantra import (  # pyright: ignore[reportMissingImports]
    MANTRA_FORMATIONS,
    best_lineup,
    parse_roles,
)


def mantra_player(name, roles, classic="A", pfc=20.0):
    player = make_player(name, ruolo=classic, pfc=pfc, tit=80.0)
    player["ruolo_mantra"] = roles
    return player


def mantra_auction(players, **overrides):
    cfg = {
        "teams": 1,
        "budget": 500,
        "game_mode": "mantra",
        "roster_size": 23,
        "mantra_formation": "4-3-3",
    }
    cfg.update(overrides)
    return Auction(players, **cfg)


def test_roles_are_canonical_multi_role_and_all_formations_are_eleven_players():
    assert list(parse_roles("w; A / Pc")) == ["W", "A", "Pc"]
    assert len(MANTRA_FORMATIONS) == 11
    assert all(len(slots) == 11 for slots in MANTRA_FORMATIONS.values())


def test_matching_uses_multi_role_player_once_in_best_position():
    players = [
        mantra_player("KEEPER", "Por", "P"),
        mantra_player("RIGHT", "Dd", "D"),
        mantra_player("CENTRE1", "Dc", "D"),
        mantra_player("CENTRE2", "Dc", "D"),
        mantra_player("LEFT", "Ds", "D"),
        mantra_player("MID1", "M", "C"),
        mantra_player("MID2", "C", "C"),
        mantra_player("MID3", "M;C", "C"),
        mantra_player("FLEX", "W;A", "A"),
        mantra_player("WING", "W", "A"),
        mantra_player("STRIKER", "Pc", "A"),
    ]
    lineup = best_lineup(players, "4-3-3")
    assert lineup["complete"]
    assert lineup["filled"] == 11
    assert len(lineup["assignments"]) == 11  # FLEX cannot be counted twice


def test_mantra_sale_uses_free_roster_not_classic_role_slots():
    player = mantra_player("WINGER", "W;A", classic="A")
    auction = mantra_auction(
        [player],
        slots={"P": 0, "D": 0, "C": 0, "A": 0},
        formation={"P": 0, "D": 0, "C": 0, "A": 0},
    )
    auction.mark_sold(auction.find("WINGER"), 10, "IO")
    assert auction.state["slots"]["IO"]["A"] == 0
    assert auction.check_invariants() == []


def test_mantra_preserves_two_goalkeeper_places():
    movements = [mantra_player(f"MOVE{i}", "C", "C", 1) for i in range(22)]
    keepers = [mantra_player(f"KEEPER{i}", "Por", "P", 1) for i in range(2)]
    auction = mantra_auction([*movements, *keepers], budget=100)
    for player in movements[:21]:
        auction.mark_sold(auction.find(player["nome"]), 1, "IO")
    with pytest.raises(SlotUnavailableError, match="almeno 2 portieri"):
        auction.mark_sold(auction.find(movements[21]["nome"]), 1, "IO")
    auction.mark_sold(auction.find("KEEPER0"), 1, "IO")
    auction.mark_sold(auction.find("KEEPER1"), 1, "IO")
    assert auction.check_invariants() == []


def test_calculator_exposes_tactical_impact_for_winger_forward():
    players = [
        mantra_player("POLY", "W;A", "A", 40),
        mantra_player("CENTRE", "Pc", "A", 35),
    ]
    auction = mantra_auction(players)
    evaluation = auction.evaluate(auction.find("POLY"), "IO")
    impact = evaluation["my_team"]["mantra"]
    assert evaluation["mantra_roles"] == ["W", "A"]
    assert impact["roles"] == ["W", "A"]
    assert impact["versatility"] == 3  # two wing slots plus central A/Pc
    assert impact["fills_hole"]
    assert impact["filled_after"] == impact["filled_before"] + 1


def test_public_feasibility_counts_only_players_with_official_mantra_roles():
    players = [
        *[mantra_player(f"P{i}", "Por", "P", 1) for i in range(4)],
        *[mantra_player(f"M{i}", "M;C", "C", 1) for i in range(42)],
        make_player("NO OFFICIAL ROLE", ruolo="A", pfc=1),
    ]
    cfg = league_config.normalize(
        {
            "teams": 2,
            "budget": 100,
            "game_mode": "mantra",
            "roster_size": 23,
            "team_names": ["IO", "T1"],
        }
    )
    assert league_config.feasibility(cfg, players).errors == []
    players.pop()
    players.pop()
    errors = league_config.feasibility(cfg, players).errors
    assert any("ruoli Mantra ufficiali" in error for error in errors)


def test_market_and_league_analysis_use_fine_grained_mantra_roles():
    players = [
        mantra_player("POLY", "W;A", "A", 40),
        mantra_player("STRIKER", "Pc", "A", 35),
        mantra_player("KEEPER", "Por", "P", 10),
    ]
    wa.engine = wa.TrendAuction(
        players,
        teams=1,
        budget=500,
        game_mode="mantra",
        roster_size=23,
        mantra_formation="4-3-3",
    )

    market = wa.api_svincolati(ruolo="W", team="IO")
    assert market["count"] == 1
    assert market["rows"][0]["name"] == "POLY"
    assert market["rows"][0]["role_display"] == "W/A"

    state = wa.api_state()
    assert [item["value"] for item in state["role_options"]] == [
        "Por",
        "Dd",
        "Ds",
        "Dc",
        "B",
        "E",
        "M",
        "C",
        "T",
        "W",
        "A",
        "Pc",
    ]
    assert {row["role"] for row in state["roles"]} == {
        "Por",
        "Dd",
        "Ds",
        "Dc",
        "B",
        "E",
        "M",
        "C",
        "T",
        "W",
        "A",
        "Pc",
    }
    assert set(wa.engine.trend()["roles"]["pfc"]) == {
        "Por",
        "Dd",
        "Ds",
        "Dc",
        "B",
        "E",
        "M",
        "C",
        "T",
        "W",
        "A",
        "Pc",
    }


def test_rose_uses_mantra_roles_for_filter_summary_and_display():
    players = [
        mantra_player("POLY", "W;A", "A", 40),
        mantra_player("STRIKER", "Pc", "A", 35),
    ]
    wa.engine = wa.TrendAuction(
        players,
        teams=1,
        budget=500,
        game_mode="mantra",
        roster_size=23,
        mantra_formation="4-3-3",
    )
    wa.api_sold(wa.SoldBody(key=wa.engine.find("POLY")["pid"], price=10, team="IO"))

    filtered = wa.api_rose(team="IO", ruolo="W")
    assert filtered["count"] == 1
    assert filtered["rows"][0]["role_display"] == "W/A"
    assert wa.api_rose(team="IO", ruolo="Pc")["count"] == 0

    summary = wa.api_rose()["summaries"][0]
    assert summary["filled_slots"] == {"ROSA": 1}
    assert summary["total_slots"] == {"ROSA": 23}
