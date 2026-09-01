"""API forward (/api/forward/*) — simulatore fase Attaccanti integrato nell'API live.

Copre il layer HTTP degli endpoint forward senza server reale: gli handler
FastAPI vengono invocati direttamente con i body pydantic (stesso stile di
test_web_auction) e i globali ``web_auction.engine`` / ``web_auction.store``
vengono isolati per test tramite fixture.

Scenario richiesti:
- GET /api/forward/snapshot: snapshot read-only (solo A nel pool, budget/
  slot A + slots_other per squadra, state_hash/event_seq), nessuna mutazione.
- POST /api/forward/simulate: deterministicita' a parita' di seed, cache
  content-addressed (hit/miss/force/no_cache), validazioni 400 (runs fuori
  bound, player_order, values con pid sconosciuto, squadra invalida, NaN),
  infeasibilita' -> 409.
- Values: default source 'my_team_value' (ranking da fantasy.score, valore
  monetario dal my_team.team_value della squadra perspective, MAI prezzo di
  mercato) vs override esplicito (source 'override').
- Mutazioni dell'asta (sold/undo/config) cambiano lo snapshot hash e
  invalidano cache e /api/forward/latest (nessun report stale servito).
- Persistenza (store event-sourced): replay deterministico -> stesso hash;
  la persistenza non cambia la semantica della simulazione.
- Invarianti: la simulazione non muta budget/slot/pool del motore.
"""

import json

import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import pid_of  # pyright: ignore[reportMissingImports]
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingImports]
from pydantic import ValidationError  # pyright: ignore[reportMissingImports]

# Slot/formation FATTIBILI per il pool di test: 3 squadre x (1P+1D+1C+2A) = 15
# slot coperti esattamente dai 15 giocatori del pool (shortfall = 0).
SLOTS = {"P": 1, "D": 1, "C": 1, "A": 2}


def make_player(nome, ruolo="A", pfc=50.0, tit=60.0, fix_contrib=0.3):
    """Giocatore nel formato del listone (stesso schema di test_web_auction)."""
    return {
        "nome": nome,
        "squadra": "SQ",
        "ruolo": ruolo,
        "pfc": float(pfc),
        "pma": float(pfc),
        "pfc_range": "",
        "pma_range": "",
        "dpfcpma": 0.0,
        "slot": 2,
        "tit": float(tit),
        "expfm": 6.5,
        "fascia": "Top",
        "status": "T",
        "pen_prob": 0.0,
        "fk_prob": 0.0,
        "tix": 50.0,
        "fix": 50.0,
        "fix_contrib": float(fix_contrib),
    }


def make_pool():
    """15 giocatori: 6 attaccanti con statistiche DIVERSE (utility diverse =>
    ranking/volue di default non banali) + copertura P/D/C per la fattibilita'."""
    players = [
        make_player(f"A{i:02d}", pfc=20 + i * 15, tit=40 + i * 10, fix_contrib=0.1 * i)
        for i in range(1, 7)
    ]
    for role in ("P", "D", "C"):
        players += [
            make_player(f"{role}{i:02d}", ruolo=role, pfc=15 - i * 5)
            for i in range(1, 4)
        ]
    return players


def build_engine(budget=100):
    """Motore di test fattibile: 3 squadre (IO, T1, T2), pool 6A+3P+3D+3C."""
    players = make_pool()
    wa.PLAYERS = players
    wa.engine = wa.TrendAuction(
        players, teams=3, budget=budget, slots=dict(SLOTS), formation=dict(SLOTS)
    )
    return wa.engine


def set_engine(players=None, **overrides):
    """Variante set_engine per scenario custom (slot A gonfiati -> infeasible)."""
    wa.PLAYERS = players if players is not None else make_pool()
    wa.engine = wa.TrendAuction(
        wa.PLAYERS,
        teams=3,
        budget=overrides.get("budget", 100),
        slots=overrides.get("slots", dict(SLOTS)),
        formation=overrides.get("formation", dict(SLOTS)),
    )
    return wa.engine


@pytest.fixture
def forward_env():
    """Isolamento per test: salva/ripristina engine+PLAYERS+store e svuota le
    cache in-process dei forward (sotto lock, come la produzione)."""
    old = (wa.engine, wa.PLAYERS, wa.store)
    wa.store = None
    with wa._FORWARD_LOCK:
        wa._FORWARD_CACHE.clear()
        wa._FORWARD_LATEST.clear()
    yield
    wa.engine, wa.PLAYERS, wa.store = old
    with wa._FORWARD_LOCK:
        wa._FORWARD_CACHE.clear()
        wa._FORWARD_LATEST.clear()


def status_of(resp):
    """Status HTTP: i successi tornano dict (200), gli errori un JSONResponse."""
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def body_of(resp):
    return (
        json.loads(bytes(resp.body).decode("utf-8"))
        if isinstance(resp, JSONResponse)
        else resp
    )


# ------------------------------------------------------------------ snapshot
class TestForwardSnapshot:
    def test_snapshot_read_only_structure(self, forward_env):
        engine = build_engine()
        p_before = dict(engine.state["money"])
        pool_before = set(engine.state["pool"])
        resp = wa.api_forward_snapshot()
        assert not isinstance(resp, JSONResponse)
        assert resp["schema_version"] == 1
        assert resp["read_only"]
        snap = resp["snapshot"]
        assert snap["schema_version"] == 1
        # solo ruolo A nel pool
        assert len(snap["pool"]) == 6
        assert all(engine.players[pid]["ruolo"] == "A" for pid in snap["pool"])
        # budget/slots A + slots_other per squadra
        teams = {t["team"]: t for t in snap["teams"]}
        assert teams["IO"]["budget"] == 100 and teams["IO"]["slots_a"] == 2
        assert teams["T1"]["budget"] == 100 and teams["T1"]["slots_a"] == 2
        assert teams["IO"]["slots_other"] == {"P": 1, "D": 1, "C": 1}
        # hash/seq coerenti
        assert len(resp["state_hash"]) == 64
        assert resp["event_seq"] == 0
        assert resp["pool_count"] == len(snap["pool"])
        assert resp["teams_count"] == 3
        # NESSUNA mutazione
        assert engine.state["money"] == p_before
        assert engine.state["pool"] == pool_before

    def test_snapshot_no_mutation_and_seq_tracking(self, forward_env):
        engine = build_engine()
        h0 = wa.api_forward_snapshot()["state_hash"]
        seq0 = wa.api_forward_snapshot()["event_seq"]
        # la richiesta stessa non muta nulla
        assert h0 == wa.api_forward_snapshot()["state_hash"]
        p = engine.find("A01")
        engine.mark_sold(p, 60, "IO")
        snap = wa.api_forward_snapshot()
        assert snap["event_seq"] == seq0 + 1
        assert snap["state_hash"] != h0
        assert len(snap["snapshot"]["pool"]) == 5  # A01 uscito

    def test_snapshot_only_attackers_even_with_mixed_pool(self, forward_env):
        build_engine()  # pool motore = 15 giocatori (A+P+D+C)
        snap = wa.api_forward_snapshot()["snapshot"]
        assert all(
            wa.engine.players[pid]["ruolo"] == "A" for pid in snap["pool"]
        )


# ------------------------------------------------------------------ simulate
class TestForwardSimulateDeterministic:
    def test_same_seed_same_result(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=25, seed=7, no_cache=True)
        )
        r2 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=25, seed=7, no_cache=True)
        )
        assert r1["result"] == r2["result"]
        assert r1["state_hash"] == r2["state_hash"]
        assert not r1["cached"] and not r2["cached"]

    def test_different_seed_different_result(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=30, seed=7, no_cache=True)
        )
        r2 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=30, seed=99, no_cache=True)
        )
        assert r1["result"] != r2["result"]

    def test_result_schema_v1_envelope(self, forward_env):
        build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=5, seed=1))
        assert not env["cached"]
        assert isinstance(env["duration_ms"], float)
        assert env["result"]["schema_version"] == 1
        assert env["generated_at"].endswith("Z")  # UTC
        assert env["event_seq"] == 0
        assert env["state_hash"]
        assert env["cache_key"]
        assert env["simulate_params"] == {
            "runs": 5,
            "seed": 1,
            "player_order": "shuffle",
            "team": "IO",
        }


class TestForwardCache:
    def test_cache_hit_on_identical_request(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        assert not r1["cached"]
        r2 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        assert r2["cached"]
        assert r1["result"] == r2["result"]
        assert r1["cache_key"] == r2["cache_key"]

    def test_force_recomputes_but_keeps_cache(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        r2 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, seed=7, force=True)
        )
        assert not r2["cached"]
        assert r1["result"] == r2["result"]  # stesso input => stesso output
        r3 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        assert r3["cached"]  # la force ha ri-popolato la cache

    def test_no_cache_bypasses_read_and_write(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, seed=7, no_cache=True)
        )
        assert not r1["cached"]
        r2 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        assert not r2["cached"]  # no_cache non ha scritto la cache

    def test_sold_changes_hash_and_misses_cache(self, forward_env):
        engine = build_engine()
        r1 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        p = engine.find("A01")
        engine.mark_sold(p, 60, "IO")
        r2 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        assert not r2["cached"]
        assert r2["state_hash"] != r1["state_hash"]
        # undo: lo stato torna identico -> cache hit sulla chiave originale
        engine.undo()
        r3 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        assert r3["cached"]
        assert r3["state_hash"] == r1["state_hash"]

    def test_config_change_changes_hash_and_misses_cache(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        build_engine(budget=90)  # nuova config -> nuovo stato
        r2 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        assert not r2["cached"]
        assert r2["state_hash"] != r1["state_hash"]


class TestForwardSimulateValidation:
    @pytest.mark.parametrize("runs", [0, -1, 50_001, 100_000])
    def test_runs_out_of_bounds(self, forward_env, runs):
        build_engine()
        resp = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=runs))
        assert status_of(resp) == 400
        assert "runs" in body_of(resp)["error"]

    def test_runs_lower_bound_accepted(self, forward_env):
        build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=1, seed=7))
        assert status_of(env) == 200
        assert env["result"]["league"]["n_players"] == 6

    def test_invalid_player_order(self, forward_env):
        build_engine()
        resp = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, player_order="random")
        )
        assert status_of(resp) == 400
        assert "player_order" in body_of(resp)["error"]

    def test_values_unknown_pid(self, forward_env):
        build_engine()
        resp = wa.api_forward_simulate(
            wa.ForwardSimulateBody(
                runs=10, values={"XYZ": wa.ForwardValueOverride(value=10.0)}
            )
        )
        assert status_of(resp) == 400
        assert "values" in body_of(resp)["error"]

    def test_values_non_attacker_pid_rejected(self, forward_env):
        engine = build_engine()
        pid = pid_of(engine, "P01")  # portiere: non nel pool A
        resp = wa.api_forward_simulate(
            wa.ForwardSimulateBody(
                runs=10, values={pid: wa.ForwardValueOverride(value=10.0)}
            )
        )
        assert status_of(resp) == 400

    def test_nan_value_rejected_by_pydantic(self, forward_env):
        build_engine()
        with pytest.raises(ValidationError):
            wa.ForwardValueOverride(value=float("nan"))

    def test_invalid_rank_rejected_by_pydantic(self, forward_env):
        build_engine()
        with pytest.raises(ValidationError):
            wa.ForwardValueOverride(rank=0)

    def test_invalid_team_perspective(self, forward_env):
        build_engine()
        resp = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, team="SQUADRA_INESISTENTE")
        )
        assert status_of(resp) == 400

    def test_empty_attacker_pool_400(self, forward_env):
        engine = build_engine()
        # slot A: 2 per squadra -> vendite distribuite su IO/T1/T2
        for nome, team in [
            ("A01", "IO"),
            ("A02", "IO"),
            ("A03", "T1"),
            ("A04", "T1"),
            ("A05", "T2"),
            ("A06", "T2"),
        ]:
            engine.mark_sold(engine.find(nome), 10, team)
        resp = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10))
        assert status_of(resp) == 400
        assert "pool" in body_of(resp)["error"]


class TestForwardInfeasible:
    def test_shortfall_409(self, forward_env):
        """Slot A totali (9) > attaccanti nel pool (6) -> infeasibile -> 409."""
        players = make_pool()
        wa.PLAYERS = players
        # costruzione fattibile, poi gonfio gli slot A come farebbe una config
        # alternativa: il simulatore legge solo lo snapshot -> shortfall
        wa.engine = wa.TrendAuction(
            players,
            teams=3,
            budget=100,
            slots={"P": 1, "D": 1, "C": 1, "A": 2},
            formation={"P": 1, "D": 1, "C": 1, "A": 2},
        )
        for name in wa.engine.state["money"]:
            wa.engine.state["slots"][name]["A"] = 3
        wa.engine.cfg["slots"]["A"] = 3
        resp = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10))
        assert status_of(resp) == 409
        data = body_of(resp)
        assert "infeasibile" in data["error"]
        assert data["feasibility"]["shortfall"] == 3

    def test_infeasible_not_cached(self, forward_env):
        players = make_pool()
        wa.PLAYERS = players
        wa.engine = wa.TrendAuction(
            players,
            teams=3,
            budget=100,
            slots={"P": 1, "D": 1, "C": 1, "A": 2},
            formation={"P": 1, "D": 1, "C": 1, "A": 2},
        )
        for name in wa.engine.state["money"]:
            wa.engine.state["slots"][name]["A"] = 3
        wa.engine.cfg["slots"]["A"] = 3
        wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10))
        assert wa._FORWARD_CACHE == {}


# ------------------------------------------------------------------- values
class TestForwardValues:
    def test_default_source_is_my_team_value(self, forward_env):
        build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        assert env["values_source"] == "my_team_value"

    def test_default_values_from_fantasy_and_team_value(self, forward_env):
        engine = build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        per_player = {p["pid"]: p for p in env["result"]["per_player"]}
        # ranking condiviso: rank 1..N distinti
        ranks = sorted(p["model_value"]["rank"] for p in per_player.values())
        assert ranks == list(range(1, len(per_player) + 1))
        # il valore monetario di default e' il my_team.team_value della squadra
        # perspective (default IO), NON il prezzo di mercato
        for pid, entry in per_player.items():
            p = engine.players[pid]
            ev = engine.evaluate(p, "IO")
            tv = ev["my_team"]["team_value"]
            assert entry["model_value"]["value"] == tv
            assert entry["model_value"]["rank"] is not None
        # il rank segue il fantasy.score (discendente)
        by_rank = sorted(per_player.values(), key=lambda p: p["model_value"]["rank"])
        scores = [
            engine.evaluate(engine.players[p["pid"]])["fantasy"]["score"]
            for p in by_rank
        ]
        assert scores == sorted(scores, reverse=True)

    def test_market_and_model_value_separate(self, forward_env):
        """Il model value (team_value) non e' il prezzo di mercato (base o
        expected): resta nel blocco market, separato per contratto WP7."""
        engine = build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        differs = 0
        for entry in env["result"]["per_player"]:
            p = engine.players[entry["pid"]]
            ev = engine.evaluate(p, "IO")
            assert entry["market"]["base"] == p["base"]
            assert entry["market"]["suggested_snapshot"] == ev["suggested"]
            assert entry["model_value"]["value"] == ev["my_team"]["team_value"]
            assert entry["model_value"]["source"] == "input"
            assert "value" not in entry["market"]
            if entry["model_value"]["value"] != ev["market"]["expected"]:
                differs += 1
        assert differs > 0  # il valore per la rosa non coincide col prezzo

    def test_override_wins_with_source_override(self, forward_env):
        engine = build_engine()
        pid = pid_of(engine, "A01")
        env = wa.api_forward_simulate(
            wa.ForwardSimulateBody(
                runs=10,
                seed=7,
                values={pid: wa.ForwardValueOverride(value=123.5, rank=1)},
            )
        )
        assert env["values_source"] == "override"
        entry = next(
            p for p in env["result"]["per_player"] if p["pid"] == pid
        )
        assert entry["model_value"]["value"] == 123.5
        assert entry["model_value"]["rank"] == 1
        # gli altri restano senza input
        others = [p for p in env["result"]["per_player"] if p["pid"] != pid]
        assert all(p["model_value"]["value"] is None for p in others)

    def test_perspective_team_changes_default_value(self, forward_env):
        engine = build_engine()
        # rendo T1 asimmetrica (ha gia' un attaccante in rosa): il team_value
        # per T1 differisce da quello per IO
        engine.mark_sold(engine.find("A06"), 50, "T1")
        pid = pid_of(engine, "A01")
        r_io = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, seed=7, no_cache=True)
        )
        r_t1 = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=10, seed=7, team="T1", no_cache=True)
        )
        assert r_t1["simulate_params"]["team"] == "T1"
        entry_io = next(
            p for p in r_io["result"]["per_player"] if p["pid"] == pid
        )
        entry_t1 = next(
            p for p in r_t1["result"]["per_player"] if p["pid"] == pid
        )
        tv_io = engine.evaluate(engine.players[pid], "IO")["my_team"][
            "team_value"
        ]
        tv_t1 = engine.evaluate(engine.players[pid], "T1")["my_team"][
            "team_value"
        ]
        assert entry_io["model_value"]["value"] == tv_io
        assert entry_t1["model_value"]["value"] == tv_t1
        assert tv_io != tv_t1  # la perspective cambia davvero il valore


# ------------------------------------------------------------------- latest
class TestForwardLatest:
    def test_latest_404_before_any_simulation(self, forward_env):
        build_engine()
        resp = wa.api_forward_latest()
        assert status_of(resp) == 404
        assert "nessun report" in body_of(resp)["error"]

    def test_latest_serves_last_report_for_current_state(self, forward_env):
        build_engine()
        r1 = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        lat = wa.api_forward_latest()
        assert not isinstance(lat, JSONResponse)
        assert lat["result"] == r1["result"]
        assert lat["state_hash"] == r1["state_hash"]

    def test_latest_never_serves_stale_report(self, forward_env):
        engine = build_engine()
        wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        # vendita: l'asta cambia -> nessun report per il nuovo stato
        engine.mark_sold(engine.find("A01"), 60, "IO")
        resp = wa.api_forward_latest()
        assert status_of(resp) == 404
        assert body_of(resp)["state_hash"] != engine.state["money"]  # sanity
        # undo: lo stato torna quello simulato -> il report torna servibile
        engine.undo()
        lat = wa.api_forward_latest()
        assert not isinstance(lat, JSONResponse)

    def test_latest_and_cache_after_config_change(self, forward_env):
        build_engine()
        wa.api_forward_simulate(wa.ForwardSimulateBody(runs=10, seed=7))
        build_engine(budget=90)
        resp = wa.api_forward_latest()
        assert status_of(resp) == 404


# ------------------------------------------------------ persistenza + invarianti
class TestForwardPersistenceAndInvariants:
    def test_persistence_replay_hash_stable(self, forward_env):
        wa.store = wa.AuctionStore(":memory:")
        engine = build_engine()
        # il replay richiede un segmento config nel log (come fa main())
        wa.store.append("league_configured", {"config": dict(engine.cfg)})
        h0 = wa.api_forward_snapshot()["state_hash"]
        # vendita persistita -> hash cambia
        resp = wa.api_sold(wa.SoldBody(key=pid_of(engine, "A01"), price=60))
        assert status_of(resp) == 200
        h1 = wa.api_forward_snapshot()["state_hash"]
        assert h1 != h0
        # undo = revoke nel log + rebuild dal replay -> hash stabile (torna a h0)
        resp = wa.api_undo()
        assert status_of(resp) == 200
        assert wa.api_forward_snapshot()["state_hash"] == h0
        # replay esplicito dal log: identita' dell'hash (replay deterministico)
        rebuilt = wa.replay_engine(wa.store, wa.PLAYERS, wa.TrendAuction)
        wa.engine = rebuilt
        assert wa.api_forward_snapshot()["state_hash"] == h0

    def test_persistence_does_not_change_simulate_semantics(self, forward_env):
        build_engine()
        in_memory = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=15, seed=7, no_cache=True)
        )
        wa.store = wa.AuctionStore(":memory:")
        with wa._FORWARD_LOCK:
            wa._FORWARD_CACHE.clear()
        persisted = wa.api_forward_simulate(
            wa.ForwardSimulateBody(runs=15, seed=7, no_cache=True)
        )
        assert persisted["result"] == in_memory["result"]
        assert persisted["state_hash"] == in_memory["state_hash"]

    def test_simulate_never_mutates_engine(self, forward_env):
        engine = build_engine()
        money0 = dict(engine.state["money"])
        slots0 = {t: dict(s) for t, s in engine.state["slots"].items()}
        pool0 = set(engine.state["pool"])
        sold0 = list(engine.state["sold"])
        for seed in (1, 2):
            wa.api_forward_simulate(
                wa.ForwardSimulateBody(runs=20, seed=seed, no_cache=True)
            )
        assert engine.state["money"] == money0
        assert engine.state["slots"] == slots0
        assert engine.state["pool"] == pool0
        assert engine.state["sold"] == sold0
        assert engine.events == []

    def test_report_budget_slots_invariants(self, forward_env):
        engine = build_engine()
        env = wa.api_forward_simulate(wa.ForwardSimulateBody(runs=20, seed=7))
        teams = {t["team"]: t for t in env["result"]["per_team"]}
        assert teams["IO"]["budget0"] == engine.state["money"]["IO"]
        assert teams["IO"]["slots_a0"] == engine.state["slots"]["IO"]["A"]
        assert env["result"]["league"]["n_teams"] == 3
        assert env["result"]["league"]["n_players"] == 6
        # lega chiusa: budget0 per squadra sommati = crediti della lega
        assert sum(t["budget0"] for t in teams.values()) == 300
