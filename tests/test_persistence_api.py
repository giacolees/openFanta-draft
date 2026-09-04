"""Test del ciclo di vita della persistenza via API (scripts/web_auction.py) — WP4.

Con store attivo (fixture ``wa.store``, DB temporaneo):
- vendita/invenduto appendono l'evento e il motore resta allineato al log;
- undo persistito = revoke dell'ultima azione attiva (mai pop silenzioso);
- POST /api/config apre un nuovo segmento ``league_configured``;
- GET /api/backup espone il log versionato; POST /api/restore (append|replace)
  valida su temp/replay PRIMA di sostituire lo stato (input invalido => 400 e
  DB/motore intatti);
- POST /api/correct (revoke|restate) con validazione su motore temporaneo;
- /api/state espone il blocco persistence (enabled, event_seq, last_saved);
- senza store (--no-store / import senza main) il comportamento resta
  in-memory e gli endpoint di persistenza rispondono 409.
"""

import json

import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from auction_store import (  # pyright: ignore[reportMissingImports]
    AuctionStore,
    StoreError,
)
from conftest import make_player  # pyright: ignore[reportMissingImports]
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingImports]
from import_listone import compute_pid  # pyright: ignore[reportMissingImports]

DEFAULT_CFG = {
    "teams": 3,
    "budget": 100,
    "slots": {"P": 3, "D": 3, "C": 3, "A": 6},
    "formation": {"P": 1, "D": 1, "C": 1, "A": 4},
    "io": "IO",
    "team_names": ["IO", "T1", "T2"],
}


def make_pool(p=0, d=0, c=0, a=8):
    """Pool di giocatori con nomi per ruolo (P01.., D01.., C01.., A01..),
    in modo da coprire le configurazioni usate dai test (feasibility publico)."""
    players = []
    for role, n in (("P", p), ("D", d), ("C", c), ("A", a)):
        players += [make_player(f"{role}{i:02d}", ruolo=role) for i in range(1, n + 1)]
    return players


# Pool che copre la config di default (3 squadre x P3 D3 C3 A6) e la config
# postata nei test (2 squadre x P3 D8 C8 A6): mai infeasible per l'API.
POOL_COVERING = make_pool(p=9, d=18, c=18, a=18)


def status_of(resp):
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def body_of(resp):
    return (
        json.loads(bytes(resp.body).decode("utf-8"))
        if isinstance(resp, JSONResponse)
        else resp
    )


@pytest.fixture
def persisted(tmp_path):
    """Web engine con store attivo su DB temporaneo e primo league_configured."""
    wa.PLAYERS = list(POOL_COVERING)
    st = AuctionStore(tmp_path / "asta.db")
    st.append("league_configured", {"config": dict(DEFAULT_CFG)})
    st.set_meta("league_cfg", dict(DEFAULT_CFG))
    wa.store = st
    wa.engine = wa.replay_engine(st, wa.PLAYERS, wa.TrendAuction)
    yield st
    wa.store = None
    wa.engine = wa.TrendAuction(
        [dict(p) for p in POOL_COVERING], teams=3, budget=100, io="IO"
    )
    st.close()


def test_api_sold_con_store_persiste_e_resume(persisted):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    assert status_of(resp) == 200
    assert body_of(resp)["ok"] is True and body_of(resp)["team"] == "IO"
    # il log ha il primo config (seq 1) + la vendita (seq 2)
    assert persisted.event_seq == 2
    last = persisted.last_action()
    assert last["type"] == "sold"
    assert last["payload"]["pid"] == compute_pid("A01", "A")
    assert persisted.latest()["supersedes"] is None
    # motore allineato al log
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.undo_stack == []  # in persistita l'undo e' event-based
    # resume: chiudi e riapri => stessa vendita ricostruita dal log
    persisted.close()
    st2 = AuctionStore(str(persisted.path))
    eng2 = wa.replay_engine(st2, wa.PLAYERS, wa.TrendAuction)
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert eng2.state["sold"] == expected
    assert eng2.check_invariants() == []
    st2.close()


def test_api_unsold_con_store_persiste(persisted):
    assert status_of(wa.api_unsold(wa.NameBody(key="A02"))) == 200
    assert persisted.event_seq == 2
    assert persisted.last_action()["type"] == "unsold"
    assert wa.engine.state["unsold"][compute_pid("A02", "A")] == 1


def test_api_sold_senza_store_resta_in_memory():
    wa.PLAYERS = make_pool()
    wa.store = None
    wa.engine = wa.TrendAuction(
        [dict(p) for p in make_pool()], teams=3, budget=100, io="IO"
    )
    assert wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))["ok"]
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected


def test_api_undo_persistito_revoca_ultima_azione(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    wa.api_sold(wa.SoldBody(key="A02", price=30, team="T1"))
    resp = wa.api_undo()
    assert status_of(resp) == 200
    assert body_of(resp)["ok"] is True and body_of(resp)["revoked"] == 3
    # la revoke e' nel log (seq 4); il motore e' ricostruito dal replay
    assert [e["type"] for e in persisted.read_all()] == [
        "league_configured",
        "sold",
        "sold",
        "revoke",
    ]
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.check_invariants() == []
    # secondo undo: revoca la prima vendita
    resp2 = wa.api_undo()
    assert body_of(resp2)["ok"] is True and body_of(resp2)["revoked"] == 2
    assert wa.engine.state["sold"] == []
    # terzo undo: nessuna azione attiva => ok False (niente pop silenzioso)
    resp3 = wa.api_undo()
    assert body_of(resp3)["ok"] is False
    assert [e["type"] for e in persisted.read_all()] == [
        "league_configured",
        "sold",
        "sold",
        "revoke",
        "revoke",
    ]


def test_api_undo_in_memory_snapshot():
    wa.PLAYERS = make_pool()
    wa.store = None
    wa.engine = wa.TrendAuction(
        [dict(p) for p in make_pool()], teams=3, budget=100, io="IO"
    )
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    assert wa.api_undo()["ok"] is True
    assert wa.engine.state["sold"] == []


def test_api_config_apre_nuovo_segmento(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    resp = wa.api_config_post(
        wa.ConfigBody(teams=2, budget=300, names=["IO", "T1"], io="IO")
    )
    assert status_of(resp) == 200
    cfg_events = [e for e in persisted.read_all() if e["type"] == "league_configured"]
    assert len(cfg_events) == 2  # storico precedente resta nel log
    # il motore riparte dall'ultima config: asta azzerata
    assert wa.engine.cfg["teams"] == 2 and wa.engine.cfg["budget"] == 300
    assert wa.engine.state["sold"] == []
    assert wa.engine.check_invariants() == []
    assert persisted.get_meta("league_cfg") is not None


def test_random_queue_persists_and_undo_restores_progression(persisted):
    resp = wa.api_config_post(
        wa.ConfigBody(
            teams=2,
            budget=300,
            names=["IO", "T1"],
            io="IO",
            auction_mode="random",
        )
    )
    assert status_of(resp) == 200
    cfg_event = persisted.latest()
    queue = cfg_event["payload"]["config"]["random_queue"]
    expected = {p.get("pid") or compute_pid(p["nome"], p["ruolo"]) for p in wa.PLAYERS}
    assert len(queue) == len(expected)
    assert set(queue) == expected
    assert wa.api_nomination()["current"]["pid"] == queue[0]

    wa.api_unsold(wa.NameBody(key=queue[0]))
    assert wa.engine.nomination_queue() == queue[1:] + [queue[0]]

    resumed = wa.replay_engine(persisted, wa.PLAYERS, wa.TrendAuction)
    assert resumed.nomination_queue() == queue[1:] + [queue[0]]

    undo = wa.api_undo()
    assert undo["nomination"]["current_pid"] == queue[0]
    assert wa.engine.nomination_queue() == queue


def test_api_state_espone_persistence(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    st = wa.api_state()
    assert st["persistence"]["enabled"] is True
    assert st["persistence"]["event_seq"] == 2
    assert st["persistence"]["last_saved"] == persisted.latest()["ts"]
    # in-memory: blocco con enabled False
    wa.store = None
    st2 = wa.api_state()
    assert st2["persistence"]["enabled"] is False
    assert st2["persistence"]["event_seq"] is None


def test_api_backup_torna_log_versionato(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    doc = wa.api_backup()
    assert doc["format"] == "openfanta-draft-events" and doc["version"] == 1
    assert [e["type"] for e in doc["events"]] == ["league_configured", "sold"]
    assert doc["events"][1]["payload"]["price"] == 40
    assert doc["persistence"]["enabled"] is True


def test_api_backup_senza_store_409():
    wa.store = None
    resp = wa.api_backup()
    assert status_of(resp) == 409
    assert "persistenza disattivata" in body_of(resp)["error"]


def test_api_restore_replace(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    # costruisce il doc di un restore "pulito" (config + vendita da 30 a T1)
    events = [
        {
            "seq": 1,
            "ts": "2026-09-01T08:00:00+00:00",
            "type": "league_configured",
            "payload": {"config": dict(DEFAULT_CFG)},
            "supersedes": None,
        },
        {
            "seq": 2,
            "ts": "2026-09-01T08:01:00+00:00",
            "type": "sold",
            "payload": {
                "pid": compute_pid("A01", "A"),
                "nome": "A01",
                "ruolo": "A",
                "price": 30,
                "team": "T1",
                "base": 50,
            },
            "supersedes": None,
        },
    ]
    resp = wa.api_restore(wa.RestoreBody(mode="replace", events=events))
    assert status_of(resp) == 200
    assert body_of(resp)["mode"] == "replace" and body_of(resp)["events"] == 2
    expected = [(compute_pid("A01", "A"), 30, "T1", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.check_invariants() == []
    assert persisted.event_seq == 2  # il log e' stato sostituito


def test_api_restore_append(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    sold2 = {
        "seq": 2,  # seq rimappata in append (il log ha gia' seq 1 e 2)
        "ts": "2026-09-01T08:05:00+00:00",
        "type": "sold",
        "payload": {
            "pid": compute_pid("A02", "A"),
            "nome": "A02",
            "ruolo": "A",
            "price": 25,
            "team": "IO",
            "base": 50,
        },
        "supersedes": None,
    }
    resp = wa.api_restore(wa.RestoreBody(mode="append", events=[sold2]))
    assert status_of(resp) == 200
    assert persisted.event_seq == 3  # config(1) + sold A01(2) + sold A02(3)
    assert wa.engine.state["sold"] == [
        (compute_pid("A01", "A"), 40, "IO", "A"),
        (compute_pid("A02", "A"), 25, "IO", "A"),
    ]
    assert wa.engine.check_invariants() == []


def test_api_restore_drift_rimuove_giocatore_da_rosa(persisted):
    events = [
        {
            "seq": 1,
            "ts": "2026-09-01T08:00:00+00:00",
            "type": "league_configured",
            "payload": {"config": dict(DEFAULT_CFG)},
            "supersedes": None,
        },
        {
            "seq": 2,
            "ts": "2026-09-01T08:01:00+00:00",
            "type": "sold",
            "payload": {
                "pid": compute_pid("SPARITO", "A"),
                "nome": "SPARITO",
                "ruolo": "A",
                "price": 30,
                "team": "T1",
                "base": 50,
            },
            "supersedes": None,
        },
    ]

    resp = wa.api_restore(wa.RestoreBody(mode="replace", events=events))

    assert status_of(resp) == 200
    assert wa.engine.state["sold"] == []
    assert wa.engine.state["money"]["T1"] == DEFAULT_CFG["budget"]
    assert wa.api_rose(team="T1")["rows"] == []
    assert persisted.event_seq == 3
    revoke = persisted.latest()
    assert revoke["type"] == "revoke"
    assert revoke["supersedes"] == 2
    assert revoke["payload"]["reason"] == "listone_drift/player_removed"


def test_api_restore_invalido_400_preserva_stato(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    before_events = persisted.read_all()
    before_sold = list(wa.engine.state["sold"])
    sold = {
        "pid": compute_pid("A02", "A"),
        "nome": "A02",
        "ruolo": "A",
        "price": 30,
        "team": "T1",
        "base": 50,
    }
    # La doppia vendita resta una violazione bloccante e non muta lo store.
    bad_events = [
        {
            "seq": 1,
            "ts": "2026-09-01T08:00:00+00:00",
            "type": "league_configured",
            "payload": {"config": dict(DEFAULT_CFG)},
            "supersedes": None,
        },
        {
            "seq": 2,
            "ts": "2026-09-01T08:01:00+00:00",
            "type": "sold",
            "payload": sold,
            "supersedes": None,
        },
        {
            "seq": 3,
            "ts": "2026-09-01T08:02:00+00:00",
            "type": "sold",
            "payload": sold,
            "supersedes": None,
        },
    ]
    resp = wa.api_restore(wa.RestoreBody(mode="replace", events=bad_events))
    assert status_of(resp) == 400
    # DB e motore intatti
    assert persisted.read_all() == before_events
    assert wa.engine.state["sold"] == before_sold
    # mode invalido / events mancante => 400
    assert status_of(wa.api_restore(wa.RestoreBody(mode="bogus", events=[]))) == 400
    assert status_of(wa.api_restore(wa.RestoreBody(events=None))) == 400


def test_api_restore_senza_store_409():
    wa.store = None
    resp = wa.api_restore(wa.RestoreBody(mode="append", events=[]))
    assert status_of(resp) == 409


def test_api_correct_restate(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    resp = wa.api_correct(
        wa.CorrectBody(target_seq=2, kind="restate", price=30, team="T1")
    )
    assert status_of(resp) == 200
    assert body_of(resp)["corrected"] == 2
    assert body_of(resp)["new"] == {"price": 30, "team": "T1"}
    assert [e["type"] for e in persisted.read_all()] == [
        "league_configured",
        "sold",
        "revoke",
        "sold",
    ]
    expected = [(compute_pid("A01", "A"), 30, "T1", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.state["money"]["IO"] == 100  # rimborsato
    assert wa.engine.state["money"]["T1"] == 70
    assert wa.engine.check_invariants() == []


def test_api_correct_revoke(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    resp = wa.api_correct(wa.CorrectBody(target_seq=2, kind="revoke"))
    assert status_of(resp) == 200
    assert body_of(resp)["revoked"] == 2
    assert wa.engine.state["sold"] == []
    assert wa.engine.check_invariants() == []


def test_api_rose_delete_persistito_revoca_vendita(persisted):
    pid = compute_pid("A01", "A")
    wa.api_sold(wa.SoldBody(key=pid, price=40, team="IO"))

    resp = wa.api_rose_delete(pid)

    assert status_of(resp) == 200
    assert resp["removed"] == {
        "pid": pid,
        "player": "A01",
        "team": "IO",
        "price": 40,
    }
    assert [event["type"] for event in persisted.read_all()] == [
        "league_configured",
        "sold",
        "revoke",
    ]
    assert persisted.latest()["supersedes"] == 2
    assert wa.engine.state["sold"] == []
    assert wa.engine.state["money"]["IO"] == 100
    assert wa.api_rose(team="IO")["rows"] == []


def test_api_correct_by_key(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    resp = wa.api_correct(
        wa.CorrectBody(key="A01", kind="restate", price=45, team="IO")
    )
    assert status_of(resp) == 200
    expected = [(compute_pid("A01", "A"), 45, "IO", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.check_invariants() == []


def test_api_correct_restate_oltre_budget_400(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    # T1 ha solo 100 cr: restate a 150 (dopo il rimborso di 40 -> 140) fallisce
    resp = wa.api_correct(
        wa.CorrectBody(target_seq=2, kind="restate", price=150, team="T1")
    )
    assert status_of(resp) == 400
    assert "budget" in body_of(resp)["error"].lower()
    # nessuna mutazione ne' sul log ne' sul motore
    assert persisted.event_seq == 2
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected


def test_api_correct_target_gia_rettificato_409(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    wa.api_correct(wa.CorrectBody(target_seq=2, kind="revoke"))
    resp = wa.api_correct(
        wa.CorrectBody(target_seq=2, kind="restate", price=30, team="IO")
    )
    assert status_of(resp) == 409
    assert "gia' rettificato" in body_of(resp)["error"]


def test_api_correct_input_invalidi_400(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    assert status_of(wa.api_correct(wa.CorrectBody(target_seq=99))) == 400
    assert (
        status_of(wa.api_correct(wa.CorrectBody(key="IO"))) == 404
    )  # squadra, non un giocatore
    assert (
        status_of(
            wa.api_correct(
                wa.CorrectBody(target_seq=1, kind="restate", price=10, team="IO")
            )
        )
        == 400
    )  # target = config
    assert (
        status_of(
            wa.api_correct(
                wa.CorrectBody(target_seq=2, kind="restate", price=-1, team="IO")
            )
        )
        == 400
    )  # prezzo non valido


def test_api_correct_senza_store_409():
    wa.store = None
    resp = wa.api_correct(wa.CorrectBody(target_seq=1, kind="revoke"))
    assert status_of(resp) == 409


def test_api_append_fallito_riallinea_motore(persisted, monkeypatch):
    # simuliamo un errore SQLite in append: il motore deve tornare al DB
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    before_seq = persisted.event_seq

    def boom(*a, **k):
        raise StoreError("scrittura simulata fallita")

    monkeypatch.setattr(persisted, "append", boom)
    resp = wa.api_sold(wa.SoldBody(key="A02", price=25, team="T1"))
    assert status_of(resp) == 500
    assert persisted.event_seq == before_seq  # DB non aggiornato
    # motore riallineato al log: la vendita fallita NON e' presente
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected
    assert wa.engine.check_invariants() == []
    monkeypatch.undo()
    # dopo il ripristino le vendite ripartono correttamente
    assert wa.api_sold(wa.SoldBody(key="A02", price=25, team="T1"))["ok"]
    assert persisted.event_seq == 3


def test_api_lifecycle_resume_dopo_riavvio(tmp_path):
    """Smoke reale: vendita persistita, store chiuso (crash), riaperto,
    motore ricostruito => la vendita c'e' e le invarianti valgono."""
    wa.PLAYERS = list(POOL_COVERING)
    db = tmp_path / "lifecycle.db"
    st = AuctionStore(db)
    st.append("league_configured", {"config": dict(DEFAULT_CFG)})
    wa.store = st
    wa.engine = wa.replay_engine(st, wa.PLAYERS, wa.TrendAuction)
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    wa.api_unsold(wa.NameBody(key="A02"))
    st.close()  # "crash"
    st2 = AuctionStore(db)
    wa.store = st2
    wa.engine = wa.replay_engine(st2, wa.PLAYERS, wa.TrendAuction)
    st = wa.api_state()
    assert st["persistence"]["enabled"] is True
    assert st["persistence"]["event_seq"] == 3
    expected = [(compute_pid("A01", "A"), 40, "IO", "A")]
    assert wa.engine.state["sold"] == expected
    assert dict(wa.engine.state["unsold"]) == {compute_pid("A02", "A"): 1}
    assert wa.engine.check_invariants() == []
    assert [e["kind"] for e in wa.engine.events] == ["sold", "unsold"]
    # trend ricostruito dal log (team canonico)
    trend = wa.api_trend()
    assert trend["events"][0]["team"] == "IO"
    st2.close()
    wa.store = None
    wa.engine = wa.TrendAuction(
        [dict(p) for p in POOL_COVERING], teams=3, budget=100, io="IO"
    )
