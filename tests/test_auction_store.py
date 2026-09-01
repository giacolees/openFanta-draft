"""Test dello store event-sourced (scripts/auction_store.py) — WP4.

Coprono, sul livello store (SQLite + replay senza HTTP):
- append/read_all con ``seq`` monotone univoche; nessun DELETE di singolo evento;
- replay deterministico: stesso log => stesso stato ed eventi (roundtrip
  byte/state equivalent, incl. backup -> import -> backup);
- resume/crash: chiudi e riapri lo store => stesso stato;
- revoke/restate (correct) e config-reset via nuovo ``league_configured``;
- backup replace/append con rimappatura ``supersedes``;
- input corrotto => atomicita' (DB intatto);
- missing pid (drift listone) e log che violano le invarianti => StoreError;
- scritture concorrenti (N thread) => seq univoci e invarianti a quiescenza;
- schema_version, WAL, context manager, store chiuso.
"""

import json
import threading

import pytest  # pyright: ignore[reportMissingImports]
from auction_store import (  # pyright: ignore[reportMissingImports]
    AuctionStore,
    StoreError,
    StoreValidationError,
    active_events,
    apply_event,
    replay_engine,
)
from conftest import PLAYERS  # pyright: ignore[reportMissingImports]
from import_listone import compute_pid  # pyright: ignore[reportMissingImports]
from web_auction import TrendAuction  # pyright: ignore[reportMissingImports]

DEFAULT_CFG = {
    "teams": 3,
    "budget": 100,
    "slots": {"P": 3, "D": 3, "C": 3, "A": 6},
    "formation": {"P": 1, "D": 1, "C": 1, "A": 4},
    "io": "IO",
    "team_names": ["IO", "T1", "T2"],
}


def _pid(nome, ruolo="A"):
    return compute_pid(nome, ruolo)


def _players():
    return [dict(q) for q in PLAYERS]


def _seed(store, cfg=None):
    """Primo ``league_configured`` (come fa web_auction.main() su DB vuoto)."""
    store.append("league_configured", {"config": cfg or dict(DEFAULT_CFG)})
    return store


def _make_store(tmp_path, name="asta.db"):
    return AuctionStore(tmp_path / name)


def _sold(nome, price=40, team="IO", ruolo="A"):
    return {
        "pid": _pid(nome, ruolo),
        "nome": nome,
        "ruolo": ruolo,
        "price": price,
        "team": team,
        "base": 50,
    }


def _unsold(nome, ruolo="A"):
    return {"pid": _pid(nome, ruolo), "nome": nome, "ruolo": ruolo}


# ---------------------------------------------------------------- append/read
def test_store_append_read_roundtrip(tmp_path):
    with _make_store(tmp_path) as s:
        _seed(s)
        s.append("sold", _sold("ALPHA"))
        s.append("unsold", _unsold("BETA"))
        evs = s.read_all()
        assert [e["type"] for e in evs] == ["league_configured", "sold", "unsold"]
        assert [e["seq"] for e in evs] == [1, 2, 3]
        assert evs[1]["payload"] == _sold("ALPHA")
        assert evs[2]["supersedes"] is None
        assert evs[1]["ts"] and "T" in evs[1]["ts"]  # ts UTC ISO
        assert s.event_seq == 3 and s.latest()["type"] == "unsold"
    # lo store non esiste piu' come risorsa: nessun evento cancellato
    with _make_store(tmp_path) as s2:
        assert [e["seq"] for e in s2.read_all()] == [1, 2, 3]


def test_store_append_never_deletes_single_event(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("revoke", {"target_seq": 2, "reason": "undo"}, supersedes=2)
    # il target resta nel log: solo il conteggio degli attivi cambia
    assert [e["seq"] for e in s.read_all()] == [1, 2, 3]
    assert len(active_events(s.read_all())) == 1  # solo league_configured
    s.close()


def test_store_append_batch_atomico(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    # una voce invalida nel batch => NIENTE viene scritto
    with pytest.raises(StoreValidationError):
        s.append_batch(
            [
                ("sold", _sold("ALPHA"), None),
                ("revoke", {"target_seq": 99, "reason": "x"}, 99),
            ]
        )
    assert s.event_seq == 1  # il vecchio config resta; il batch e' rollbackato
    # batch valido => seq contigue in un comando logico
    seqs = s.append_batch(
        [("sold", _sold("ALPHA"), None), ("unsold", _unsold("BETA"), None)]
    )
    assert seqs == [2, 3]
    s.close()


def test_store_revoke_target_deve_essere_sold_unsold(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    with pytest.raises(StoreValidationError):
        s.append("revoke", {"target_seq": 1, "reason": "x"}, supersedes=1)  # config!
    with pytest.raises(StoreValidationError):
        s.append(
            "revoke", {"target_seq": 2, "reason": "x"}, supersedes=2
        )  # inesistente
    s.append("sold", _sold("ALPHA"))
    s.append("revoke", {"target_seq": 2, "reason": "ok"}, supersedes=2)
    # double revoke dello stesso target => rifiutata (gia' rettificata)
    with pytest.raises(StoreValidationError):
        s.append("revoke", {"target_seq": 2, "reason": "again"}, supersedes=2)
    s.close()


# ------------------------------------------------------------ replay determin.
def test_replay_roundtrip_state_equivalent(tmp_path):
    s1 = _make_store(tmp_path, "a.db")
    _seed(s1)
    s1.append("sold", _sold("ALPHA"))
    s1.append("unsold", _unsold("BETA"))
    s1.append("sold", _sold("GAMMA", price=30, team="T1"))
    base = _players()
    eng1 = replay_engine(s1, base, TrendAuction)
    # stesso log su un motore fresco (secondo store identico) => stesso stato
    s2 = _make_store(tmp_path, "b.db")
    _seed(s2)
    for e in s1.read_all()[1:]:  # salta il primo config (gia' seedato)
        s2.append(e["type"], e["payload"], e["supersedes"])
    eng2 = replay_engine(s2, base, TrendAuction)
    assert eng1.state == eng2.state
    assert eng1.events == eng2.events
    assert eng1.check_invariants() == [] and eng2.check_invariants() == []
    assert eng1.state["sold"] == [
        (_pid("ALPHA"), 40, "IO", "A"),
        (_pid("GAMMA"), 30, "T1", "A"),
    ]
    assert dict(eng1.state["unsold"]) == {_pid("BETA"): 1}
    s1.close()
    s2.close()


def test_replay_byte_equivalent_backup_import(tmp_path):
    s1 = _make_store(tmp_path, "a.db")
    _seed(s1)
    s1.append("sold", _sold("ALPHA"))
    s1.append("unsold", _unsold("BETA"))
    b1 = tmp_path / "backup1.json"
    s1.backup(b1)
    s2 = _make_store(tmp_path, "b.db")
    s2.import_events(b1, mode="replace")
    # byte-equivalenza: backup del secondo store identico al primo
    b2 = tmp_path / "backup2.json"
    s2.backup(b2)
    assert b1.read_bytes() == b2.read_bytes()
    # read_all identici dopo l'import replace
    evs1 = s1.read_all()
    evs2 = s2.read_all()
    assert evs1 == evs2
    # i payload sono gli stessi dict (JSON canonico): valore dello store uguale
    assert [e["payload"] for e in evs1] == [e["payload"] for e in evs2]
    s1.close()
    s2.close()


def test_backup_import_replace_rigenera_meta(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.set_meta("league_cfg", dict(DEFAULT_CFG))
    b = tmp_path / "b.json"
    s.backup(b)
    doc = json.loads(b.read_text())
    assert doc["version"] == 1 and doc["format"] == "openfanta-draft-events"
    assert doc["meta"]["schema_version"] == "1"
    assert "league_cfg" in doc["meta"]
    s2 = _make_store(tmp_path, "b.db")
    s2.import_events(b, mode="replace")
    assert s2.get_meta("league_cfg") == s.get_meta("league_cfg")
    s.close()
    s2.close()


def test_backup_append_mode_rimappa_supersedes(tmp_path):
    # log A: config + sold + unsold + revoke dell'unsold (supersedes = 3)
    s = _make_store(tmp_path, "a.db")
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("unsold", _unsold("BETA"))
    s.append("revoke", {"target_seq": 3, "reason": "undo"}, supersedes=3)
    b = tmp_path / "b.json"
    s.backup(b)
    # log B: solo config (store diverso, puo' avere gia' altri eventi)
    s2 = _make_store(tmp_path, "b.db")
    _seed(s2)
    s2.import_events(b, mode="append")
    evs = s2.read_all()
    assert [e["seq"] for e in evs] == [1, 2, 3, 4, 5]
    # la revoke appesa punta (rimappata) all'unsold BETA appena importato
    rev = next(e for e in evs if e["type"] == "revoke")
    assert rev["supersedes"] == 4
    assert evs[3]["type"] == "unsold" and evs[3]["payload"]["pid"] == _pid("BETA")
    # replay del risultato: invarianti verdi, l'unsold BETA e' revocato
    eng = replay_engine(s2, _players(), TrendAuction)
    assert eng.check_invariants() == []
    assert dict(eng.state["unsold"]) == {}
    expected = [(_pid("ALPHA"), 40, "IO", "A")]
    assert eng.state["sold"] == expected
    s.close()
    s2.close()


def test_backup_import_file_atomico_su_input_corrotto(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    before = s.read_all()
    bad = tmp_path / "bad.json"
    bad.write_text("{ non-json")
    with pytest.raises(StoreValidationError):
        s.import_events(bad, mode="replace")
    bad2 = tmp_path / "bad2.json"
    bad2.write_text(json.dumps({"version": 2, "events": []}))
    with pytest.raises(StoreValidationError):
        s.import_events(bad2, mode="replace")
    bad3 = tmp_path / "bad3.json"
    bad3.write_text(
        json.dumps(
            {
                "format": "openfanta-draft-events",
                "version": 1,
                "events": [
                    {
                        "seq": 1,
                        "ts": "2026-09-01T00:00:00+00:00",
                        "type": "sold",
                        "payload": {"pid": "x"},  # payload incompleto
                        "supersedes": None,
                    }
                ],
            }
        )
    )
    with pytest.raises(StoreValidationError):
        s.import_events(bad3, mode="replace")
    # DB intatto dopo ogni tentativo invalido
    assert s.read_all() == before
    s.close()


def test_resume_after_reopen_crash_like(tmp_path):
    p = tmp_path / "asta.db"
    s = AuctionStore(p)
    _seed(s)
    s.append("sold", _sold("ALPHA", price=40, team="IO"))
    s.append("unsold", _unsold("BETA"))
    # "crash": chiudiamo senza alcun flush oltre le COMMIT gia' fatte
    s.close()
    s = AuctionStore(p)  # riapri (resume)
    eng = replay_engine(s, _players(), TrendAuction)
    expected = [(_pid("ALPHA"), 40, "IO", "A")]
    assert eng.state["sold"] == expected
    assert dict(eng.state["unsold"]) == {_pid("BETA"): 1}
    assert eng.check_invariants() == []
    s.close()


def test_replay_pulisce_undo_stack(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("sold", _sold("BETA", price=30, team="T1"))
    eng = replay_engine(s, _players(), TrendAuction)
    assert eng.undo_stack == []  # mai snapshot ricostruiti dopo il replay
    assert [e["kind"] for e in eng.events] == ["sold", "sold"]
    s.close()


# ----------------------------------------------------------- revoke / restate
def test_revoke_unsold_conta_contatore(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("unsold", _unsold("ALPHA"))
    s.append("unsold", _unsold("ALPHA"))
    # revoca della prima (seq 2): resta la seconda => contatore 1
    s.append("revoke", {"target_seq": 2, "reason": "undo"}, supersedes=2)
    eng = replay_engine(s, _players(), TrendAuction)
    assert dict(eng.state["unsold"]) == {_pid("ALPHA"): 1}
    assert eng.check_invariants() == []
    s.close()


def test_restate_revoke_piu_sold_state_finale(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA", price=40, team="IO"))
    s.append_batch(
        [
            ("revoke", {"target_seq": 2, "reason": "correct/restate"}, 2),
            ("sold", _sold("ALPHA", price=30, team="T1"), None),
        ]
    )
    eng = replay_engine(s, _players(), TrendAuction)
    expected = [(_pid("ALPHA"), 30, "T1", "A")]
    assert eng.state["sold"] == expected
    assert eng.state["money"]["IO"] == 100  # rimborsato
    assert eng.state["money"]["T1"] == 70
    assert eng.check_invariants() == []
    # storico integro: il target originale resta nel log
    assert s.event_by_seq(2)["type"] == "sold"
    assert len([e for e in s.read_all() if e["type"] == "revoke"]) == 1
    s.close()


def test_config_reset_via_nuovo_evento(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("sold", _sold("BETA", price=30, team="T1"))
    # nuova config: apre un nuovo segmento, lo storico precedente resta nel log
    new_cfg = dict(DEFAULT_CFG, budget=200)
    s.append("league_configured", {"config": new_cfg})
    eng = replay_engine(s, _players(), TrendAuction)
    assert eng.cfg["budget"] == 200
    assert eng.state["sold"] == [] and eng.state["pool"] == set(eng.players)
    assert eng.check_invariants() == []
    assert len([e for e in s.read_all() if e["type"] == "league_configured"]) == 2
    s.close()


# ---------------------------------------------------------------- input invalido
def test_log_corrotto_doppia_vendita_errore(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("sold", _sold("ALPHA"))  # anti-invariante: append lo accetta (struttura)
    with pytest.raises(StoreError, match="giocatore|invarianti|sold"):
        replay_engine(s, _players(), TrendAuction)
    s.close()


def test_missing_pid_drift_listone_errore(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ZZZ-GIOCATORE-SPARITO"))
    with pytest.raises(StoreError, match="drift|assente dal listone"):
        replay_engine(s, _players(), TrendAuction)
    s.close()


def test_replay_senza_league_configured_errore(tmp_path):
    s = _make_store(tmp_path)
    s.append("sold", _sold("ALPHA"))  # log senza config
    with pytest.raises(StoreError, match="league_configured"):
        replay_engine(s, _players(), TrendAuction)
    s.close()


def test_log_senza_config_ma_solo_revoke_errore(tmp_path):
    s = AuctionStore(tmp_path / "x.db")
    with pytest.raises(StoreError):
        replay_engine(s, _players(), TrendAuction)  # log vuoto
    s.close()


# -------------------------------------------------------------- concurrency
def test_concurrent_append_seq_univoci(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    names = ["ALPHA", "BETA", "GAMMA", "DELTA"]
    N_THREADS = 8
    PER = 10

    def worker(idx):
        for j in range(PER):
            nome = names[(idx + j) % len(names)]
            s.append("unsold", _unsold(nome))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    evs = s.read_all()
    seqs = [e["seq"] for e in evs]
    assert len(seqs) == 1 + N_THREADS * PER
    assert seqs == list(range(1, len(seqs) + 1))  # monotone, senza buchi
    assert len(set(seqs)) == len(seqs)  # univoche
    eng = replay_engine(s, _players(), TrendAuction)
    assert eng.check_invariants() == []
    s.close()


# ----------------------------------------------------------------- ciclo vita
def test_store_context_manager_e_close():
    s = AuctionStore(":memory:")
    with s:
        _seed(s)
        assert s.is_open
    assert not s.is_open
    with pytest.raises(StoreError, match="chiuso"):
        s.read_all()


def test_schema_version_e_wal(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    assert s.get_meta("schema_version") == "1"
    mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    # schema piu' recente del supportato => errore chiaro
    s.set_meta("schema_version", "99")
    s.close()
    with pytest.raises(StoreError, match="schema"):
        AuctionStore(tmp_path / "asta.db")


def test_store_set_meta_json_canonico(tmp_path):
    s = _make_store(tmp_path)
    s.set_meta("league_cfg", dict(DEFAULT_CFG))
    assert s.get_meta("league_cfg") == json.dumps(
        DEFAULT_CFG, sort_keys=True, separators=(",", ":")
    )
    s.close()


def test_apply_event_direttamente_su_revoke_errore(tmp_path):
    s = _make_store(tmp_path)
    _seed(s)
    s.append("sold", _sold("ALPHA"))
    s.append("revoke", {"target_seq": 2, "reason": "x"}, supersedes=2)
    eng = replay_engine(s, _players(), TrendAuction)  # il replay filtra le revoke
    assert eng.check_invariants() == []
    # apply_event esplicito su una revoke => errore contestualizzato (mai da usare)
    with pytest.raises(StoreError, match="revoke"):
        apply_event(eng, {"seq": 99, "type": "revoke", "payload": {"target_seq": 2}})
    s.close()
