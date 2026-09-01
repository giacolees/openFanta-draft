#!/usr/bin/env -S uv run --quiet python
"""Persistenza SQLite event-sourced dell'asta (WP4).

L'asta diventa **ripristinabile**: log di eventi append-only in SQLite,
replay deterministico, rettifica compensativa (revoke/restate), backup/import,
resume alla ripartenza. Unica fonte di verita' = il log; il motore (TrendAuction /
Auction) e' ricostruito ogni volta dal replay del log.

Schema (WAL, foreign keys, versione):

    events(id INTEGER PRIMARY KEY, seq INTEGER UNIQUE, ts TEXT, type TEXT,
           payload TEXT NOT NULL /*JSON canonico*/, supersedes INTEGER NULL)
    meta(key  TEXT PRIMARY KEY, value TEXT)      # schema_version, league_cfg

Contratti:

- **Append-only compensativo**: mai DELETE di un singolo evento. La rettifica
  appende un evento ``revoke`` (``supersedes`` -> seq del target) ed
  eventualmente una nuova ``sold`` (restate) in una transazione. Lo storico
  resta integro; il replay esclude gli eventi revocati (superseded).
- **Replay deterministico**: ``replay_engine``/``replay_events`` partono
  dall'ultimo ``league_configured`` e riapplicano ``sold``/``unsold`` attivi in
  ordine, verificando ``check_invariants()`` dopo ogni evento. Errori sempre
  contestualizzati; mai stato parziale (il motore viene sostituito solo dopo un
  replay riuscito).
- **Concorrenza**: lock in-process (``threading.RLock``) + ``BEGIN IMMEDIATE``
  (single writer). ``seq`` monotono e univoco. (Multi-process = hardening WP12;
  qui il modello e' single-process.)
- **Backup/import**: JSON versionato, scrittura file ATOMICA (temp+rename),
  ``mode="append"`` (remappa le ``seq``/``supersedes`` correttamente) oppure
  ``mode="replace"`` (sostituzione atomica). Input invalido => DB intatto.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

# Nessun ciclo: live_auction non importa auction_store; qui le eccezioni di
# dominio servono a contestualizzare gli errori di replay.
from live_auction import ROLE_ORDER, AuctionError
from typing_extensions import Self

SCHEMA_VERSION = 1
EVENT_TYPES = ("league_configured", "sold", "unsold", "revoke")
BACKUP_FORMAT = "openfanta-draft-events"


class StoreError(Exception):
    """Errore generico dello store (SQLite, replay, schema)."""


class StoreValidationError(StoreError):
    """Input invalido (payload, backup/import/restore): DB e stato rimangono intatti."""


def canonical_json(obj: Any) -> str:
    """Serializzazione JSON canonica (chiavi ordinate, no spazi): usata per i
    payload in modo che backup -> import -> backup siano byte-identici."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_payload(raw: str, seq: int) -> dict[str, Any]:
    """Payload JSON canonico -> dict. Il payload e' sempre canonicalizzato allo
    store; un eventuale valore malformato e' un errore di integrita' del log."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StoreError(f"evento {seq}: payload corrotto nel log: {e}") from e
    if not isinstance(data, dict):
        raise StoreError(f"evento {seq}: payload non valido nel log")
    return data


# ---------------------------------------------------------------------------
# evoluzione del log: eventi attivi / replay
# ---------------------------------------------------------------------------
def active_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Eventi ATTIVI: esclude quelli riferiti da un ``supersedes`` (revocati).

    Nel modello corrente solo ``sold``/``unsold`` sono revocabili e nulla
    revoca una ``revoke``, quindi la regola diretta (escluso se riferito da un
    qualsiasi ``supersedes``) coincide con la chiusura transitiva. I check di
    import/append impediscono catene di revoke invalide. Gli eventi ``revoke``
    sono marcatori: NON si ri-applicano (il loro effetto e' "rendere inattivo
    il target", gia' gestito escludendo i target); vengono quindi esclusi
    anche loro dagli eventi attivi da riproiettare.
    """
    superseded = {e["supersedes"] for e in events if e["supersedes"] is not None}
    return [e for e in events if e["seq"] not in superseded and e["type"] != "revoke"]


def apply_event(auction: Any, ev: dict[str, Any]) -> None:
    """Dispatcher deterministico: ri-issua un singolo evento sul motore.

    ``sold``/``unsold`` vengono ri-issua su TrendAuction/Auction attraverso le
    invarianti di dominio; ``league_configured`` e ``revoke`` non si applicano
    in replay (la prima e' usata per la costruzione, la seconda e' filtrata).
    Dopo ogni evento ``check_invariants()`` deve essere vuota, altrimenti
    StoreError contestualizzato: il replay non lascia mai stato parziale.
    """
    typ = ev["type"]
    payload = ev["payload"]
    seq = ev["seq"]
    if typ == "revoke":
        raise StoreError(
            f"evento {seq}: revoke non applicabile in replay (deve essere filtrata "
            "prima — le revoke rendono inattivo il target)"
        )
    if typ == "league_configured":
        raise StoreError(
            f"evento {seq}: league_configured e' gestita solo nella costruzione del replay"
        )
    ctx_name = payload.get("nome") or payload.get("pid") or "-"
    try:
        if typ == "sold":
            pid = payload["pid"]
            p = auction.players.get(pid)
            if p is None:
                raise StoreError(
                    f"replay {seq} (sold): giocatore {pid!r} ({ctx_name}) assente dal "
                    "listone corrente — il listone e' cambiato dall'asta (drift)"
                )
            eval_before = auction.evaluate(p, payload.get("team"))
            auction.mark_sold(p, payload["price"], payload.get("team"), eval_before)
        elif typ == "unsold":
            pid = payload["pid"]
            p = auction.players.get(pid)
            if p is None:
                raise StoreError(
                    f"replay {seq} (unsold): giocatore {pid!r} ({ctx_name}) assente dal "
                    "listone corrente — il listone e' cambiato dall'asta (drift)"
                )
            auction.mark_unsold(p)
        else:
            raise StoreError(f"evento {seq}: tipo sconosciuto {typ!r}")
    except StoreError:
        raise
    except AuctionError as e:
        raise StoreError(f"replay {seq} ({typ}): {e}") from e
    problems = auction.check_invariants()
    if problems:
        raise StoreError(
            f"replay {seq} ({typ}): invarianti violate: {'; '.join(problems)}"
        )


def replay_events(
    events: list[dict[str, Any]], players: list[Any], engine_cls: Any
) -> Any:
    """Ricostruisce il motore da una lista esplicita di eventi (replay).

    Parte dall'ULTIMO ``league_configured`` attivo e riapplica ``sold``/
    ``unsold`` attivi in ordine di ``seq`` su un motore fresco. Ricostruisce
    anche events/trend (via TrendAuction, che registra gli eventi con la squadra
    canonica durante il replay) e infine **pulisce l'undo stack** (il replay non
    passa dall'undo snapshot-based). Alza StoreError contestualizzato; non
    lascia mai stato parziale.
    """
    active = list(active_events(events))
    cfg_events = [e for e in active if e["type"] == "league_configured"]
    if not cfg_events:
        raise StoreError(
            "log vuoto o senza league_configured: impossibile ricostruire lo stato"
        )
    cfg_ev = cfg_events[-1]  # max seq = segmento piu' recente
    cfg = cfg_ev["payload"]["config"]
    if not isinstance(cfg, dict):
        raise StoreError(
            f"league_configured {cfg_ev['seq']}: payload['config'] non valido"
        )
    try:
        engine = engine_cls(players, **cfg)
    except Exception as e:
        raise StoreError(
            f"replay config ({cfg_ev['seq']}): configurazione non valida o non "
            f"sostenibile dal listone corrente: {e}"
        ) from e
    for ev in active:
        if ev["seq"] <= cfg_ev["seq"] or ev["type"] == "league_configured":
            continue
        apply_event(engine, ev)
    engine.undo_stack.clear()
    return engine


def replay_engine(store: AuctionStore, players: list[Any], engine_cls: Any) -> Any:
    """Ricostruisce il motore dal log dello store (resume deterministico)."""
    if store is None:
        raise StoreError("store non attivo")
    return replay_events(store.read_all(), players, engine_cls)


# ---------------------------------------------------------------------------
# AuctionStore — SQLite append-only
# ---------------------------------------------------------------------------
class AuctionStore:
    """Log eventi dell'asta su SQLite (WAL, foreign keys, schema_version).

    Single-process, single-writer: ``threading.RLock`` in-process + transazioni
    ``BEGIN IMMEDIATE``. Ogni ``append``/``append_batch``/import e' atomico.
    """

    def __init__(self, path: str):
        self.path = str(path)
        parent = os.path.dirname(self.path)
        if parent and parent != ":memory:" and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                raise StoreError(
                    f"caricamento store: impossibile creare la directory {parent}: {e}"
                ) from e
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.isolation_level = None  # transazioni esplicite (BEGIN/COMMIT)
        self._closed = False
        self._init_schema()

    # ------------------------------------------------------------- ciclo vita
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")  # fuori transazione
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._begin()
            try:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS events ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " seq INTEGER NOT NULL UNIQUE,"
                    " ts TEXT NOT NULL,"
                    " type TEXT NOT NULL,"
                    " payload TEXT NOT NULL,"
                    " supersedes INTEGER NULL)"
                )
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta ("
                    " key TEXT PRIMARY KEY,"
                    " value TEXT NOT NULL)"
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')"
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise
        self._check_schema()

    def _check_schema(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise StoreError("meta non inizializzata (schema corrotta)")
        try:
            v = int(row[0])
        except (TypeError, ValueError):
            raise StoreError(f"schema_version non valida: {row[0]!r}") from None
        if v > SCHEMA_VERSION:
            raise StoreError(
                f"schema del DB (v{v}) piu' recente di quello supportato "
                f"(v{SCHEMA_VERSION}): aggiornare lo store"
            )

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("store chiuso")

    @property
    def is_open(self) -> bool:
        return not self._closed

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._conn.close()
                finally:
                    self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - solo difensivo
        with contextlib.suppress(Exception):
            self.close()

    # ---------------------------------------------------------- accesso stato
    @property
    def event_seq(self) -> int:
        with self._lock:
            self._ensure_open()
            return self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events"
            ).fetchone()[0]

    @property
    def last_saved(self) -> str | None:
        ev = self.latest()
        return ev["ts"] if ev else None

    # -------------------------------------------------------------- append
    def append(
        self, type_: str, payload: dict[str, Any], supersedes: int | None = None
    ) -> int:
        """Appende un singolo evento, atomicamente; ritorna la ``seq`` assegnata."""
        return self.append_batch([(type_, payload, supersedes)])[0]

    def append_batch(
        self, events: list[tuple[str, dict[str, Any], int | None]]
    ) -> list[int]:
        """Appende una lista di eventi in UNA transazione (atomicita' del comando
        logico: e.g. restate = revoke + sold). Ritorna le ``seq`` assegnate
        (monotone univoche). Validazione strutturale + semantica delegata a
        ``_validate_event``; su errore nulla viene scritto."""
        with self._lock:
            self._ensure_open()
            self._begin()
            try:
                cur = self._conn
                base = cur.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events"
                ).fetchone()[0]
                seqs: list[int] = []
                for i, (typ, payload, sup) in enumerate(events, start=1):
                    seq = base + i
                    self._validate_event(typ, payload, sup, cur)
                    cur.execute(
                        "INSERT INTO events(seq,ts,type,payload,supersedes) "
                        "VALUES(?,?,?,?,?)",
                        (seq, _now_ts(), typ, canonical_json(payload), sup),
                    )
                    seqs.append(seq)
                self._commit()
            except StoreError:
                self._rollback()
                raise
            except sqlite3.Error as e:
                self._rollback()
                raise StoreError(f"scrittura SQLite fallita: {e}") from e
            return seqs

    def _validate_event(
        self, typ: str, payload: dict[str, Any], sup: int | None, cur: Any
    ) -> None:
        """Validazione di un singolo evento prima dell'insert (supersedes inclusa)."""
        self._validate_payload(typ, payload)
        if typ == "revoke":
            target = payload["target_seq"]
            if sup != target:
                raise StoreValidationError(
                    f"revoke: supersedes deve puntare a target_seq ({sup} != {target})"
                )
            row = cur.execute(
                "SELECT seq,type FROM events WHERE seq=?", (target,)
            ).fetchone()
            if row is None:
                raise StoreValidationError(f"revoke: target_seq {target} inesistente")
            if row[1] not in ("sold", "unsold"):
                raise StoreValidationError(
                    f"revoke: target {target} non e' una vendita/invenduto"
                )
            if cur.execute(
                "SELECT 1 FROM events WHERE supersedes=?", (target,)
            ).fetchone():
                raise StoreValidationError(f"revoke: target {target} gia' rettificato")

    def _validate_payload(self, typ: str, payload: dict[str, Any]) -> None:
        if typ not in EVENT_TYPES:
            raise StoreValidationError(f"tipo evento sconosciuto: {typ!r}")
        if not isinstance(payload, dict):
            raise StoreValidationError("payload non valido: atteso un oggetto")
        if typ == "league_configured":
            if not isinstance(payload.get("config"), dict):
                raise StoreValidationError(
                    "league_configured: payload['config'] (dict) richiesto"
                )
        elif typ == "sold":
            for k in ("pid", "nome", "ruolo", "price", "team", "base"):
                if k not in payload:
                    raise StoreValidationError(f"sold: payload manca '{k}'")
            if type(payload["price"]) is not int or payload["price"] < 1:
                raise StoreValidationError("sold: price deve essere un intero >= 1")
            if payload["ruolo"] not in ROLE_ORDER:
                raise StoreValidationError(
                    f"sold: ruolo non valido {payload['ruolo']!r}"
                )
        elif typ == "unsold":
            for k in ("pid", "nome", "ruolo"):
                if k not in payload:
                    raise StoreValidationError(f"unsold: payload manca '{k}'")
            if payload["ruolo"] not in ROLE_ORDER:
                raise StoreValidationError(
                    f"unsold: ruolo non valido {payload['ruolo']!r}"
                )
        elif typ == "revoke":
            target = payload.get("target_seq")
            if type(target) is not int or target < 1:
                raise StoreValidationError(
                    "revoke: target_seq deve essere un intero >= 1"
                )

    # --------------------------------------------------------------- letture
    def read_all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT seq,ts,type,payload,supersedes FROM events ORDER BY seq"
            ).fetchall()
        out = []
        for r in rows:
            payload = _parse_payload(r[3], r[0])
            out.append(
                {
                    "seq": r[0],
                    "ts": r[1],
                    "type": r[2],
                    "payload": payload,
                    "supersedes": r[4],
                }
            )
        return out

    def event_by_seq(self, seq: int) -> dict[str, Any] | None:
        if type(seq) is not int or seq < 1:
            return None
        with self._lock:
            self._ensure_open()
            r = self._conn.execute(
                "SELECT seq,ts,type,payload,supersedes FROM events WHERE seq=?",
                (seq,),
            ).fetchone()
        if r is None:
            return None
        return {
            "seq": r[0],
            "ts": r[1],
            "type": r[2],
            "payload": _parse_payload(r[3], r[0]),
            "supersedes": r[4],
        }

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            r = self._conn.execute(
                "SELECT seq,ts,type,payload,supersedes FROM events "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        if r is None:
            return None
        return {
            "seq": r[0],
            "ts": r[1],
            "type": r[2],
            "payload": _parse_payload(r[3], r[0]),
            "supersedes": r[4],
        }

    def last_action(self) -> dict[str, Any] | None:
        """Ultima azione ATTIVA (sold/unsold non revocata): base dell'undo persistito."""
        events = self.read_all()
        superseded = {e["supersedes"] for e in events if e["supersedes"] is not None}
        for e in reversed(events):
            if e["seq"] not in superseded and e["type"] in ("sold", "unsold"):
                return e
        return None

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def read_all_meta(self) -> dict[str, str]:
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute("SELECT key,value FROM meta").fetchall()
        return dict(rows)

    def set_meta(self, key: str, value: Any) -> None:
        """Aggiorna una chiave di ``meta`` atomicamente. I dict/list sono
        serializzati come JSON canonico; il resto come stringa."""
        text = canonical_json(value) if isinstance(value, (dict, list)) else str(value)
        with self._lock:
            self._ensure_open()
            self._begin()
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, text)
                )
                self._commit()
            except sqlite3.Error as e:
                self._rollback()
                raise StoreError(f"meta: {e}") from e

    # -------------------------------------------------------------- backup
    def backup(self, path: str) -> str:
        """Dump JSON versionato degli eventi (+ meta) su file, in modo ATOMICO
        (temp + rename). Non tocca il DB. Il payload e' serializzato in forma
        canonica: backup -> import -> backup sono byte-identici."""
        events = self.read_all()
        meta = self.read_all_meta()
        doc: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "version": SCHEMA_VERSION,
            "generated_at": _now_ts(),
            "meta": meta,
            "events": [
                {
                    "seq": e["seq"],
                    "ts": e["ts"],
                    "type": e["type"],
                    "payload": e["payload"],
                    "supersedes": e["supersedes"],
                }
                for e in events
            ],
        }
        data = canonical_json(doc) + "\n"
        path = str(path)
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".backup-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return path

    # -------------------------------------------------------------- import
    def import_events(self, path: str, mode: str = "append") -> dict[str, Any]:
        """Importa un backup da file (``mode="append"|"replace"``), atomico.

        Legge il file ("replace" totalmente atomico anche su JSON invalido:
        un file illeggibile o malformato non tocca il DB)."""
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise StoreValidationError(f"backup non leggibile ({path}): {e}") from e
        return self.import_data(doc, mode=mode)

    def import_data(self, doc: Any, mode: str = "append") -> dict[str, Any]:
        """Valida il documento e lo applica atomicamente.

        - ``replace``: sostituisce TUTTI gli eventi e la meta (restore pieno).
        - ``append``: aggiunge al log corrente, rimappando ``seq``/``supersedes``.

        La validazione completa (struttura + ``supersedes`` riferiti) avviene
        PRIMA di qualsiasi scrittura: input invalido => DB intatto. L'evento di
        una eventuale revoke si riferisce a un target sold/unsold esistente.
        """
        if mode not in ("append", "replace"):
            raise StoreValidationError(f"mode non valido: {mode!r} (append|replace)")
        events = self._validate_doc(doc)
        if mode == "replace":
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else None
            self._apply_replace(events, meta)
            return {"events": len(events), "mode": "replace"}
        n = self._apply_append(events)
        return {"events": n, "mode": "append"}

    def _validate_doc(self, doc: Any) -> list[dict[str, Any]]:
        if not isinstance(doc, dict):
            raise StoreValidationError("doc non valido: atteso un oggetto JSON")
        ver = doc.get("version")
        if ver != SCHEMA_VERSION:
            raise StoreValidationError(
                f"version non supportata: {ver!r} (attesa {SCHEMA_VERSION})"
            )
        fmt = doc.get("format")
        if fmt is not None and fmt != BACKUP_FORMAT:
            raise StoreValidationError(f"format non riconosciuto: {fmt!r}")
        raw_events = doc.get("events")
        if not isinstance(raw_events, list):
            raise StoreValidationError("events non valido: attesa una lista")
        events: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise StoreValidationError("evento non valido: atteso un oggetto")
            seq = raw.get("seq")
            if type(seq) is not int or seq < 1:
                raise StoreValidationError(f"evento seq non valida: {seq!r}")
            if seq in seen:
                raise StoreValidationError(f"seq duplicata nel backup: {seq}")
            seen.add(seq)
            typ = raw.get("type")
            if typ not in EVENT_TYPES:
                raise StoreValidationError(f"tipo evento sconosciuto: {typ!r}")
            payload = raw.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as e:
                    raise StoreValidationError(
                        f"payload non JSON valido (seq {seq}): {e}"
                    ) from None
            if not isinstance(payload, dict):
                raise StoreValidationError(
                    f"payload non valido (seq {seq}): atteso un oggetto"
                )
            self._validate_payload(typ, payload)
            sup = raw.get("supersedes")
            if sup is not None and (type(sup) is not int or sup < 1 or sup == seq):
                raise StoreValidationError(
                    f"supersedes non valida (seq {seq}): {sup!r}"
                )
            ts = raw.get("ts")
            events.append(
                {
                    "seq": seq,
                    "ts": str(ts) if ts is not None else _now_ts(),
                    "type": typ,
                    "payload": payload,
                    "supersedes": sup,
                }
            )
        meta = doc.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise StoreValidationError("meta non valido: atteso un oggetto")
        if isinstance(meta, dict):
            sv = meta.get("schema_version")
            if sv is not None:
                try:
                    if int(sv) > SCHEMA_VERSION:
                        raise StoreValidationError(
                            f"backup schema v{sv} > supportata v{SCHEMA_VERSION}"
                        )
                except (TypeError, ValueError):
                    raise StoreValidationError(
                        f"schema_version non valida: {sv!r}"
                    ) from None
        return events

    def _apply_append(self, events: list[dict[str, Any]]) -> int:
        """Append con rimappatura ``seq``/``supersedes`` (input gia' validato)."""
        with self._lock:
            self._ensure_open()
            self._begin()
            try:
                cur = self._conn
                existing = dict(cur.execute("SELECT seq,type FROM events").fetchall())
                base = max(existing) if existing else 0
                ordered = sorted(events, key=lambda e: e["seq"])
                old_to_new = {
                    ev["seq"]: base + i for i, ev in enumerate(ordered, start=1)
                }
                merged_types: dict[int, str] = {}
                merged_types.update(existing)
                merged_types.update(
                    {old_to_new[ev["seq"]]: ev["type"] for ev in ordered}
                )
                rows = []
                for ev in ordered:
                    new_seq = old_to_new[ev["seq"]]
                    new_sup = ev["supersedes"]
                    if new_sup is not None:
                        if new_sup in old_to_new:
                            new_sup = old_to_new[new_sup]  # riferimento interno
                        elif new_sup in existing:
                            pass  # riferimento a un evento gia' presente
                        else:
                            raise StoreValidationError(
                                f"supersedes {ev['supersedes']} inesistente"
                            )
                        if merged_types.get(new_sup) not in ("sold", "unsold"):
                            raise StoreValidationError(
                                f"supersedes {ev['supersedes']}: target non "
                                "rettificabile"
                            )
                    rows.append(
                        (
                            new_seq,
                            ev["ts"],
                            ev["type"],
                            canonical_json(ev["payload"]),
                            new_sup,
                        )
                    )
                cur.executemany(
                    "INSERT INTO events(seq,ts,type,payload,supersedes) "
                    "VALUES(?,?,?,?,?)",
                    rows,
                )
                self._commit()
            except StoreError:
                self._rollback()
                raise
            except sqlite3.Error as e:
                self._rollback()
                raise StoreError(f"import SQLite fallito: {e}") from e
            return len(events)

    def _apply_replace(self, events: list[dict[str, Any]], meta: dict | None) -> None:
        """Sostituzione atomica di TUTTI gli eventi e della meta (restore)."""
        with self._lock:
            self._ensure_open()
            self._begin()
            try:
                cur = self._conn
                seq_types = {ev["seq"]: ev["type"] for ev in events}
                for ev in events:
                    sup = ev["supersedes"]
                    if sup is not None:
                        if sup not in seq_types:
                            raise StoreValidationError(
                                f"supersedes {sup} inesistente nel set sostitutivo"
                            )
                        if seq_types[sup] not in ("sold", "unsold"):
                            raise StoreValidationError(
                                f"supersedes {sup}: target non rettificabile"
                            )
                cur.execute("DELETE FROM events")
                cur.executemany(
                    "INSERT INTO events(seq,ts,type,payload,supersedes) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (
                            ev["seq"],
                            ev["ts"],
                            ev["type"],
                            canonical_json(ev["payload"]),
                            ev["supersedes"],
                        )
                        for ev in sorted(events, key=lambda e: e["seq"])
                    ],
                )
                cur.execute("DELETE FROM meta")
                cur.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
                if meta:
                    for k, v in meta.items():
                        if k == "schema_version":
                            continue
                        cur.execute(
                            "INSERT INTO meta(key,value) VALUES(?,?)",
                            (
                                k,
                                canonical_json(v)
                                if isinstance(v, (dict, list))
                                else str(v),
                            ),
                        )
                self._commit()
            except StoreError:
                self._rollback()
                raise
            except sqlite3.Error as e:
                self._rollback()
                raise StoreError(f"restore SQLite fallito: {e}") from e
