#!/usr/bin/env -S uv run --quiet python
"""
GUI FastAPI per l'asta live — Fantacalcio Stagione 2026/27 (Listone Fantaculo).

Riusa il motore di scripts/live_auction.py (PFC come valore base, inflazione, scarsità
su slot consigliati, indici TIX/FIX, copertura ruolo) e aggiunge:
- inserimento rapido delle offerte da browser;
- DUE analizzatori di tendenza:
    * vs PFC: quanto stiamo pagando i giocatori rispetto al prezzo suggerito Fantaculo;
    * vs PMA: quanto stiamo pagando rispetto al prezzo medio delle aste in Italia.
  Per ognuno: media mobile delle ultime vendite, confronto col periodo precedente e
  verdetto IN RIALZO / STABILI / IN CALO, globale e per ruolo.

Avvio:
  uv run scripts/import_listone.py        # aggiorna data/listone.csv dall'ultimo xlsx
  uv run scripts/web_auction.py           # http://127.0.0.1:8000
  uv run scripts/web_auction.py --port 8080 --squadre 8 --budget 500

API: GET /api/state | GET /api/players?q= | GET /api/eval?key=&team= |
     POST /api/sold | POST /api/unsold | POST /api/undo | GET /api/trend |
     GET /api/export/svincolati?ruolo=P|D|C|A | GET /api/export/rose |
     GET /api/forward/snapshot | POST /api/forward/simulate |
     GET /api/forward/latest  (simulatore forward fase Attaccanti)
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

# Moduli fratelli in scripts/: risolti perche' 1) esecuzione come script
# (sys.path[0] = scripts/) oppure 2) import dai test (conftest aggiunge scripts/
# a sys.path prima di importare). Nessuna istruzione prima degli import (E402).
import calibration
import league_config

# WP4: store event-sourced. ``auction_store`` non importa web_auction (nessun
# ciclo): qui riusiamo replay/active_events/StoreError per la persistenza.
from auction_store import (
    AuctionStore,
    StoreError,
    active_events,
    replay_engine,
    replay_events,
)
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from forward_agg import report as forward_report
from forward_agg import snapshot_key as forward_snapshot_key
from forward_bidding import BidConfig
from forward_sim import simulate as forward_simulate
from forward_state import (
    SCHEMA_VERSION as FORWARD_SCHEMA_VERSION,
)
from forward_state import (
    snapshot_from_auction,
)
from forward_state import (
    validate_snapshot as forward_validate_snapshot,
)
from live_auction import (
    DEFAULTS,
    ROLE_LABEL,
    ROLE_ORDER,
    AmbiguousName,
    Auction,
    AuctionError,
    ConfigError,
    InvalidPriceError,
    InvalidTeamError,
    compute_pid,
    load_players,
    norm,
)
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))  # fallback difensivo
DEFAULT_CSV = f"{BASE_DIR}/data/listone.csv"
INDEX_HTML = os.path.join(BASE_DIR, "scripts", "static", "index.html")
DEFAULT_STORE_DB = f"{BASE_DIR}/data/asta_stagione_2026_27.db"

VERDICT_IT = {
    "rise": "IN RIALZO ↗",
    "fall": "IN CALO ↘",
    "stable": "STABILI →",
    "nodata": "DATI INSUFFICIENTI",
}


class TrendAuction(Auction):
    """Auction + cronologia eventi e DUE analisi di tendenza: vs PFC e vs PMA."""

    ROLLING_WINDOW = 5

    def __init__(self, players, **overrides):
        super().__init__(players, **overrides)
        self.events = []
        self.replay_drift_removed: list[dict[str, Any]] = []
        queue = self.cfg.get("random_queue")
        if self.cfg.get("auction_mode") == "random" and set(queue or []) != set(
            self.players
        ):
            raise ConfigError(
                "random_queue non valida: deve contenere esattamente tutti i pid del listone"
            )

    def nomination_queue(self):
        if self.cfg.get("auction_mode") != "random":
            return []
        queue = list(self.cfg["random_queue"])
        for event in self.events:
            pid = event["pid"]
            if pid in queue:
                queue.remove(pid)
            if event["kind"] == "unsold" and pid in self.state["pool"]:
                queue.append(pid)
        return queue

    def nomination_summary(self):
        queue = self.nomination_queue()
        current = queue[0] if queue else None
        return {
            "mode": self.cfg.get("auction_mode", "manual"),
            "current_pid": current,
            "remaining": len(queue)
            if self.cfg.get("auction_mode") == "random"
            else len(self.state["pool"]),
            "complete": not self.state["pool"],
        }

    # ---------------------------------------------------------- calibrazione
    @property
    def calibration_report(self):
        """Report di calibrazione (WP8, advisory) dagli eventi ATTIVI del motore.

        Compute on demand: deterministico, nessuno stato nascosto. ``events``
        contiene SOLO azioni attive (il replay filtra le revoke, l'undo in
        memoria fa pop), quindi il report riflette esattamente le vendite
        correnti della lega."""
        return calibration.calibration_from_events(self.events, self.cfg)

    def calibration_advisory(self, p, expected=None):
        """Advisory di calibrazione per il giocatore ``p`` (advisory-only, WP8).

        Mai applicato al prezzo: ``applied=False`` con
        ``reason="advisory_gate_off"`` finche' il gate del WP9 non lo consente."""
        return calibration.estimate_player(
            self.calibration_report, p, expected=expected
        )

    def _record(self, kind, p, price=None, eval_before=None, team=None):
        e = {
            "i": len(self.events),
            "kind": kind,
            "pid": p["pid"],
            "nome": p["nome"],
            "ruolo": p["ruolo"],
            "infl": round(self.inflation(), 4),
        }
        if kind == "sold":
            e.update(
                {
                    "price": price,
                    "team": team,  # squadra canonica ("IO", "T1", ... oppure "ALTRO")
                    "base": p["base"],
                    "premium_pfc": round(price / p["base"], 4),
                    "premium_pma": round(price / p["pma"], 4) if p["pma"] else None,
                }
            )
            if eval_before and eval_before.get("suggested"):
                e["premium_model"] = round(price / eval_before["suggested"], 4)
        self.events.append(e)

    def mark_sold(self, p, price, team, eval_before=None):
        """Vendita con squadra canonica registrata anche nell'evento:
        gli eventi restano allineati a state['sold'] senza indici paralleli."""
        team = super().mark_sold(p, price, team)
        self._record("sold", p, price, eval_before, team)
        return team

    def mark_unsold(self, p):
        super().mark_unsold(p)
        self._record("unsold", p)

    def undo(self):
        ok = super().undo()
        if ok and self.events:
            self.events.pop()
        return ok

    # ------------------------------------------------------------- tendenza
    @staticmethod
    def _rolling(values, window):
        out = []
        for i in range(len(values)):
            window_vals = values[max(0, i - window + 1) : i + 1]
            out.append(sum(window_vals) / len(window_vals))
        return out

    @staticmethod
    def _verdict(premiums, window):
        if len(premiums) >= 2 * window:
            w = window
            recent = sum(premiums[-w:]) / w
            prev = sum(premiums[-2 * w : -w]) / w
            delta = recent - prev
            v = "rise" if delta > 0.05 else "fall" if delta < -0.05 else "stable"
            return {
                "verdict": v,
                "delta": round(delta, 3),
                "recent": round(recent, 3),
                "prev": round(prev, 3),
            }
        return {"verdict": "nodata", "n": len(premiums), "needed": 2 * window}

    def trend(self):
        sales = [e for e in self.events if e["kind"] == "sold"]
        w = self.ROLLING_WINDOW
        rolls = {}
        for key in ("premium_pfc", "premium_pma"):
            vals = [e[key] for e in sales if e.get(key)]
            rolls[key] = self._rolling(vals, w)
        roll_iters = {k: iter(v) for k, v in rolls.items()}
        for e in self.events:
            if e["kind"] == "sold":
                for key in ("premium_pfc", "premium_pma"):
                    if e.get(key) is not None:
                        e[f"roll_{key}"] = round(next(roll_iters[key]), 4)

        verdicts = {
            "pfc": self._verdict(
                [e["premium_pfc"] for e in sales if e.get("premium_pfc")], w
            ),
            "pma": self._verdict(
                [e["premium_pma"] for e in sales if e.get("premium_pma")], w
            ),
        }

        roles = {}
        for key in ("pfc", "pma"):
            col = "premium_" + key
            roles[key] = {}
            for role in ROLE_ORDER:
                rp = [e[col] for e in sales if e["ruolo"] == role and e.get(col)]
                if len(rp) >= 4:
                    w2 = min(w, len(rp) // 2)
                    recent = sum(rp[-w2:]) / w2
                    prev = sum(rp[-2 * w2 : -w2]) / w2
                    delta = recent - prev
                    roles[key][role] = {
                        "verdict": "rise"
                        if delta > 0.05
                        else "fall"
                        if delta < -0.05
                        else "stable",
                        "delta": round(delta, 3),
                        "n": len(rp),
                    }
                else:
                    roles[key][role] = {"verdict": "nodata", "n": len(rp)}
        return {
            "events": self.events,
            "verdict": verdicts,
            "roles": roles,
            "window": self.ROLLING_WINDOW,
        }


def role_state(engine, role):
    cfg = engine.cfg
    group = [
        engine.players[k]
        for k in engine.state["pool"]
        if engine.players[k]["ruolo"] == role
    ]
    usable = [p for p in group if p["base"] >= cfg["quality_floor"]]
    scarcities = sorted(engine.scarcity(p) for p in usable)
    med = scarcities[len(scarcities) // 2] if scarcities else 1.0
    spent = sum(price for _, price, _, r in engine.state["sold"] if r == role)
    return {
        "role": role,
        "label": ROLE_LABEL[role],
        "demand": engine._demand(role),
        "usable": len(usable),
        "pool": len(group),
        "scarcity_med": round(med, 2),
        "quality_left": engine.quality_left(role),
        "starters_left": engine.starters_left(role),
        "starters_needed": cfg["teams"] * cfg["formation"][role],
        "fix_mean": round(engine.fix_mean(role), 2),
        "spent": spent,
        "spent_pct": round(100 * spent / max(engine.role_pool0[role], 1)),
    }


def player_payload(engine, p, team, calib=None):
    """Payload del giocatore dall'engine (WP8 advisory: ``calib`` riusato).

    L'argomento ``calib`` (report di calibrazione gia' calcolato) evita di
    ricomputarlo per ogni giocatore di una stessa richiesta."""
    e = engine.evaluate(p, team)
    if "error" in e:
        e = {**e, "suggested": None}
    if calib is None:
        calib = engine.calibration_report
    return {
        "key": p[
            "pid"
        ],  # API: la chiave e' il pid (accetta anche i nomi legacy in input)
        "pid": p["pid"],
        "nome": p["nome"],
        "ruolo": p["ruolo"],
        "ruolo_label": ROLE_LABEL[p["ruolo"]],
        "squadra": p["squadra"],
        "base": p["base"],
        "pfc_range": p["pfc_range"],
        "pfc_lo": p.get("pfc_lo"),
        "pfc_hi": p.get("pfc_hi"),
        "pma": p["pma"],
        "pma_range": p["pma_range"],
        "pma_lo": p.get("pma_lo"),
        "pma_hi": p.get("pma_hi"),
        "unc_pfc": p.get("unc_pfc"),
        "dpfcpma": p["dpfcpma"],
        "slot": p["slot"],
        "tit": p["tit"],
        "expfm": p["expfm"],
        "tix": p["tix"],
        "fix": p["fix"],
        "fix_contrib": p["fix_contrib"],
        "fascia": p["fascia"],
        "status": p["status"],
        "pen_prob": p["pen_prob"],
        "fk_prob": p["fk_prob"],
        "unsold": engine.state["unsold"].get(p["pid"], 0),
        **e,
        # WP5: campi per-singola squadra (additivi, mai sul suggerito/maxbid)
        "scarcity_team": e.get("scarcity_team"),
        "scarcity_breakdown": e.get("scarcity_breakdown"),
        # WP6: maxbid a 4 cap trasparenti (``maxbid`` top-level == final)
        "maxbid_breakdown": e.get("maxbid_breakdown"),
        # WP7: contratto esplicito a tre blocchi disgiunti (market | fantasy |
        # my_team) — nessun indice unico; additivo, None sul percorso errore
        "market": e.get("market"),
        "fantasy": e.get("fantasy"),
        "my_team": e.get("my_team"),
        # WP8: advisory di calibrazione (fattore di mercato cella x fase). Mai
        # applicato al prezzo: applied=False, reason=advisory_gate_off.
        "calibration": calibration.estimate_player(
            calib, p, expected=e.get("suggested")
        ),
    }


app = FastAPI(title="Asta Live Fanta 2026/27 — Listone Fantaculo")
engine: TrendAuction = None  # type: ignore[assignment]  # inizializzato in main()
PLAYERS: list[dict[str, Any]] = []  # listone caricato una sola volta (main()/fixture)
# WP4: store event-sourced attivo quando il sorgente e' avviato senza --no-store.
# None = modalita' in-memory (CLI/tests che importano senza main()): il ciclo di
# vita delle API resta identico al comportamento pre-persistenza.
store: AuctionStore | None = None


def require_players() -> list[dict[str, Any]]:
    """Giocatori del listone, obbligatori per validare una config: alza se non
    caricati (main() li inizializza prima di servire; mai None nel tipo)."""
    if not PLAYERS:
        raise RuntimeError(
            "listone non caricato: avvia web_auction.main() prima di POST /api/config"
        )
    return PLAYERS


# ------------------------------------------------------------- persistenza
# Il DB e' l'unica fonte di verita': il motore e' un cache ricostrutta dal
# replay del log. Ogni mutazione persistita appende l'evento DOPO la validazione
# sul motore (che e' transazionale: valida prima di mutare); se l'append fallisce
# ripristiniamo il motore dal DB (rollback+rebuild) cosi' non esiste mai un caso
# "DB aggiornato / stato del motore vecchio" o viceversa.


def _persistence_block() -> dict[str, Any]:
    if store is None:
        return {"enabled": False, "event_seq": None, "last_saved": None, "path": None}
    return {
        "enabled": True,
        "event_seq": store.event_seq,
        "last_saved": store.last_saved,
        "path": store.path,
    }


def _rebuild_engine_from_store() -> None:
    """Ricomincia il motore dal log (DB = unica fonte di verita')."""
    global engine
    if store is None:
        raise StoreError("store non attivo")
    engine = replay_engine(store, PLAYERS, TrendAuction)


def _validate_restore(events: list[dict[str, Any]], meta, mode: str) -> None:
    """Valida un restore su uno store temporaneo in memoria e lo fa replayare
    integralmente su un motore fresco PRIMA di toccare il DB ufficiale. Alza
    StoreError se il risultato non e' ricostruibile: in quel caso DB e motore
    restano intatti (mai stato parziale)."""
    if store is None:
        raise StoreError("store non attivo")
    tmp = AuctionStore(":memory:")
    try:
        for e in store.read_all():
            tmp.append(e["type"], e["payload"], e["supersedes"])
        doc: dict[str, Any] = {
            "format": "openfanta-draft-events",
            "version": 1,
            "events": events,
        }
        if meta is not None:
            doc["meta"] = meta
        tmp.import_data(doc, mode=mode)
        replay_engine(tmp, PLAYERS, TrendAuction)
    finally:
        tmp.close()


def _persist(
    ev_type: str, payload: dict[str, Any], supersedes: int | None = None
) -> None:
    """Command helper WP4: appende l'evento della mutazione gia' validata sul
    motore. Su errore ripristina il motore dal DB e rilancia; in modalita'
    persistita svuota lo snapshot-undo (l'undo diventa event-based). Con store
    inattivo e' un no-op (in-memory conserva l'undo snapshot-based)."""
    if store is None:
        return
    try:
        store.append(ev_type, payload, supersedes)
    except StoreError:
        _rebuild_engine_from_store()
        raise
    engine.undo_stack.clear()


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        with open(INDEX_HTML, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return Response("index.html mancante", status_code=404, media_type="text/plain")


@app.get("/api/state")
def api_state(team: str | None = None):
    team = team or engine.cfg["io"]
    infl = engine.inflation()
    return {
        "inflation": round(infl, 3),
        "inflation_pma": round(engine.inflation_pma(), 3),
        "io": engine.cfg["io"],
        "money_left": engine.state["money_league"],
        "value_left": engine._value_rem(),
        "total_value0": engine.total_value0,
        "formation": engine.cfg["formation"],
        "tit_threshold": engine.cfg["tit_cov_threshold"],
        "roles": [role_state(engine, r) for r in ROLE_ORDER],
        "budgets": sorted(engine.state["money"].items(), key=lambda kv: -kv[1]),
        "spent_unknown": engine.state["spent_unknown"],
        "sales": sum(1 for e in engine.events if e["kind"] == "sold"),
        "events": len(engine.events),
        "nomination": engine.nomination_summary(),
        "persistence": _persistence_block(),
    }


class ConfigBody(BaseModel):
    teams: int
    budget: int
    names: list[str]
    io: str | None = None
    slots: dict[str, int] | None = None
    formation: dict[str, int] | None = None
    tit_cov_threshold: int | None = None
    use_calibration_in_price: bool | None = None
    auction_mode: str = "manual"


def config_payload(engine):
    """Config normalizzata del motore corrente (contratto di GET/POST /api/config)."""
    cfg = engine.cfg
    return {
        "teams": cfg["teams"],
        "budget": cfg["budget"],
        "names": cfg["team_names"],
        "io": cfg["io"],
        "slots": dict(cfg["slots"]),
        "formation": dict(cfg["formation"]),
        "tit_cov_threshold": cfg["tit_cov_threshold"],
        "auction_mode": cfg.get("auction_mode", "manual"),
        # WP8: flag advisory (esposto ma NON applicato al prezzo in questo WP)
        "use_calibration_in_price": _use_calibration_flag(cfg),
    }


@app.get("/api/config")
def api_config():
    """Espone l'intera configurazione normalizzata della lega corrente."""
    return config_payload(engine)


@app.post("/api/config")
def api_config_post(body: ConfigBody):
    """Riconfigura lega (crediti, squadre, nomi, slot/formation, soglia titolarita'):
    azzera l'asta SOLO se la configurazione e' valida e fattibile.

    Validazione centralizzata in league_config (profilo pubblico, stesso schema
    di CLI e dominio): strutturale (limiti, nomi, slot/formation) + fattibilita'
    (pool per ruolo, budget vs costo minimo rosa). Se la configurazione viene
    rifiutata (400 sulla lista errori) il motore corrente e lo stato restano
    intatti: nessuna config invalida puo' azzerare l'asta.
    Risposta: config normalizzata, esito feasibility (errori+warnings) e costo
    minimo della rosa.
    """
    global engine
    io = (body.io or (body.names[0] if body.names else "")).strip()
    overrides = {
        "teams": body.teams,
        "budget": body.budget,
        "io": io,
        "team_names": [n.strip() for n in body.names if n.strip()],
    }
    if body.slots is not None:
        overrides["slots"] = body.slots
    if body.formation is not None:
        overrides["formation"] = body.formation
    if body.tit_cov_threshold is not None:
        overrides["tit_cov_threshold"] = body.tit_cov_threshold
    if body.use_calibration_in_price is not None:
        overrides["use_calibration_in_price"] = body.use_calibration_in_price
    overrides["auction_mode"] = body.auction_mode
    if body.auction_mode == "random":
        queue = sorted(
            p.get("pid") or compute_pid(p["nome"], p["ruolo"])
            for p in require_players()
        )
        random.SystemRandom().shuffle(queue)
        overrides["random_queue"] = queue
    else:
        overrides["random_queue"] = None
    cfg = league_config.normalize(overrides)
    errors = list(league_config.validate(cfg))
    if errors:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)
    players = require_players()
    feat = league_config.feasibility(cfg, players)
    if feat.errors:
        return JSONResponse({"error": "; ".join(feat.errors)}, status_code=400)
    # Prima validazione del motore (profilo engine) su una config GI&A" applicata
    # solo se accettata: cosi' un eventuale fallimento non tocca ne' il motore
    # corrente ne' il log.
    try:
        new_engine = TrendAuction(players, **cfg)
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if store is not None:
        # WP4: la riconfigurazione apre un NUOVO segmento: si appende un nuovo
        # ``league_configured`` (lo storico precedente resta nel log, il replay
        # riparte dall'ultima config). Il motore viene poi ricostruito dal replay.
        try:
            store.append("league_configured", {"config": dict(cfg)})
            store.set_meta("league_cfg", dict(cfg))
        except StoreError as e:
            _rebuild_engine_from_store()
            return JSONResponse({"error": str(e)}, status_code=500)
        _rebuild_engine_from_store()
    else:
        engine = new_engine
    return {
        "ok": True,
        "config": config_payload(engine),
        "feasibility": feat.as_dict(),
        "minimum_roster_cost": league_config.minimum_roster_cost(cfg, players),
        "teams": engine.cfg["teams"],
        "budget": engine.cfg["budget"],
        "names": engine.cfg["team_names"],
        "io": engine.cfg["io"],
        "nomination": engine.nomination_summary(),
        "persistence": _persistence_block(),
    }


def nomination_payload(team: str | None = None):
    summary = engine.nomination_summary()
    current_pid = summary["current_pid"]
    return {
        "mode": summary["mode"],
        "current": (
            player_payload(engine, engine.players[current_pid], team)
            if current_pid is not None
            else None
        ),
        "remaining": summary["remaining"],
        "complete": summary["complete"],
    }


@app.get("/api/nomination")
def api_nomination(team: str | None = None):
    return nomination_payload(team)


@app.get("/api/players")
def api_players(q: str = "", team: str | None = None, limit: int = 12):
    qn = norm(q)
    pool = engine.state["pool"]

    def _match(pid):
        if pid not in pool:
            return False
        if not qn:
            return True
        # ricerca per nome normalizzato (substring) o per pid (substring)
        return qn in norm(engine.players[pid]["nome"]) or qn in pid

    matches = [pid for pid in engine.players if _match(pid)]
    matches.sort(key=lambda k: -engine.players[k]["base"])
    return {
        "results": [
            player_payload(engine, engine.players[k], team) for k in matches[:limit]
        ]
    }


@app.get("/api/eval")
def api_eval(key: str, team: str | None = None):
    try:
        p = engine.find(key)
    except AmbiguousName as e:
        return JSONResponse({"error": f"nome ambiguo: {e}"}, status_code=400)
    if p is None:
        return JSONResponse({"error": "giocatore non trovato"}, status_code=404)
    return player_payload(engine, p, team, calib=engine.calibration_report)


class SoldBody(BaseModel):
    key: str
    price: int
    team: str | None = None


class NameBody(BaseModel):
    key: str


def _domain_error_status(exc):
    """HTTP status per gli errori di dominio: input malformato -> 400,
    conflitto di stato (budget/slot/pool) -> 409. Mai 500 per una
    violazione di regola d'asta."""
    if isinstance(exc, (InvalidPriceError, InvalidTeamError)):
        return 400
    return 409


@app.post("/api/sold")
def api_sold(body: SoldBody):
    try:
        p = engine.find(body.key)
    except AmbiguousName as e:
        return JSONResponse({"error": f"nome ambiguo: {e}"}, status_code=400)
    if p is None:
        return JSONResponse({"error": "giocatore non trovato"}, status_code=404)
    try:
        eval_before = engine.evaluate(p, body.team)
        team = engine.mark_sold(p, body.price, body.team, eval_before)
    except AuctionError as e:
        return JSONResponse({"error": str(e)}, status_code=_domain_error_status(e))
    try:
        _persist(
            "sold",
            {
                "pid": p["pid"],
                "nome": p["nome"],
                "ruolo": p["ruolo"],
                "price": body.price,
                "team": team,
                "base": p["base"],
            },
        )
    except StoreError as e:
        # append fallito: il motore e' gia' stato riallineato al DB (rollback)
        return JSONResponse({"error": str(e)}, status_code=500)
    return {
        "ok": True,
        "pid": p["pid"],
        "nome": p["nome"],
        "team": team,
        "premium_pfc": round(body.price / p["base"], 3),
        "premium_pma": round(body.price / p["pma"], 3) if p["pma"] else None,
        "nomination": engine.nomination_summary(),
        "persistence": _persistence_block(),
    }


@app.post("/api/unsold")
def api_unsold(body: NameBody):
    try:
        p = engine.find(body.key)
    except AmbiguousName as e:
        return JSONResponse({"error": f"nome ambiguo: {e}"}, status_code=400)
    if p is None:
        return JSONResponse({"error": "giocatore non trovato"}, status_code=404)
    try:
        engine.mark_unsold(p)
    except AuctionError as e:
        return JSONResponse({"error": str(e)}, status_code=_domain_error_status(e))
    try:
        _persist(
            "unsold",
            {"pid": p["pid"], "nome": p["nome"], "ruolo": p["ruolo"]},
        )
    except StoreError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {
        "ok": True,
        "pid": p["pid"],
        "nome": p["nome"],
        "nomination": engine.nomination_summary(),
    }


@app.post("/api/undo")
def api_undo():
    """Undo in modalita' persistita = revoke dell'ultima azione ATTIVA (sold/unsold
    non gia' revocata): niente pop silenzioso dello snapshot, il log resta
    append-only. In-memory conserva l'undo snapshot-based."""
    if store is not None:
        target = store.last_action()
        if target is None:
            return {"ok": False, "persistence": _persistence_block()}
        try:
            store.append(
                "revoke",
                {"target_seq": target["seq"], "reason": "undo"},
                supersedes=target["seq"],
            )
        except StoreError as e:
            _rebuild_engine_from_store()
            return JSONResponse({"error": str(e)}, status_code=500)
        _rebuild_engine_from_store()
        return {
            "ok": True,
            "revoked": target["seq"],
            "nomination": engine.nomination_summary(),
            "persistence": _persistence_block(),
        }
    ok = engine.undo()
    return {"ok": ok, "nomination": engine.nomination_summary()}


def _use_calibration_flag(cfg: dict[str, Any]) -> bool:
    """Flag booleano ``use_calibration_in_price`` dalla config (default False).

    L'intento (WP8) e' booleano: si accetta solo True; ogni altro valore
    (assente, oppure tuning numerico descritto come bool) vale False. Il flag
    e' SOLO esposto: non entra mai nel prezzo in questo WP (advisory gate OFF)."""
    raw = cfg.get("use_calibration_in_price")
    return isinstance(raw, bool) and raw


@app.get("/api/calibration")
def api_calibration():
    """Report di calibrazione (WP8) dagli eventi ATTIVI del motore: celle
    (ruolo x banda x fase) con n/n_eff, mediana ponderata, MAD e CI bounded,
    gerarchia (globale/ruolo/ruolo-fase) e fase corrente. ADVISORY: un flag
    ``use_calibration_in_price`` (esposto) resta non applicato in questo WP.
    Ritorna il dict del report + il flag e ``advisory=True``."""
    data = engine.calibration_report.to_dict()
    data["use_calibration_in_price"] = _use_calibration_flag(engine.cfg)
    data["advisory"] = True
    data["reason"] = "advisory_gate_off"
    return data


@app.get("/api/trend")
def api_trend():
    data = engine.trend()
    data["verdict_labels"] = {
        k: VERDICT_IT[v["verdict"]] for k, v in data["verdict"].items()
    }
    data["role_labels"] = {
        k: {r: VERDICT_IT[v["verdict"]] for r, v in roles.items()}
        for k, roles in data["roles"].items()
    }
    return data


# ==================================================================
# Simulatore forward (fase Attaccanti) — WP integrato nell'API live.
#
# Snapshot read-only dall'engine (solo ruolo A nel pool) + simulazione Monte
# Carlo deterministica con cache content-addressed in-process. Reusa i moduli
# del core forward (forward_state.forward_sim.forward_agg) senza toccare il
# motore live: nessun import circolare, nessuna mutazione dell'asta.
#
# Separazione market/model (contratto WP7):
# - blocco "market" del report: solo input di prezzo (base/PMA/suggested0/
#   scarc0/sim_avg_price) — MAI value/rank;
# - blocco "model_value": solo value/rank in ingresso. Default (source
#   "my_team_value"): ranking condiviso dal blocco fantasy (score, fallback
#   utility) e valore monetario dal blocco my_team.team_value della squadra
#   perspective — MAI il prezzo di mercato come model value;
# - override esplicito (body.values) prevale sempre (source "override").
#
# Cache: chiave = sha256 del JSON canonico (snapshot con values baked-in +
# cfg), come la CLI forward_simulator. Un acquisto/undo/correct/config cambia
# lo snapshot hash e causa miss automatico (content-addressed). Il lock
# serializza SOLO l'accesso alle mappe; la simulazione gira FUORI dal lock:
# due richieste identiche possono calcolare due volte, ma non corrompono
# mai la cache (put idempotente, stesso valore per stessa chiave).

FORWARD_MIN_RUNS = 1
FORWARD_MAX_RUNS = 50_000
FORWARD_DEFAULT_RUNS = 10_000
FORWARD_VALUE_SOURCE_DEFAULT = "my_team_value"
FORWARD_VALUE_SOURCE_OVERRIDE = "override"

_FORWARD_CACHE: dict[str, dict[str, Any]] = {}
_FORWARD_LATEST: dict[str, dict[str, Any]] = {}  # state_hash -> ultimo report
_FORWARD_LOCK = threading.Lock()


class ForwardValueOverride(BaseModel):
    """Override per un pid: value monetario modello e/o rank (INPUT modello,
    mai dentro la formula di prezzo — vedi forward_bidding)."""

    value: float | None = Field(default=None, allow_inf_nan=False)
    rank: int | None = Field(default=None, ge=1)


class ForwardSimulateBody(BaseModel):
    """Body tipizzato di POST /api/forward/simulate."""

    runs: int = FORWARD_DEFAULT_RUNS
    seed: int = 42
    player_order: str = "shuffle"  # "shuffle" | "by_value"
    team: str | None = None  # squadra perspective (default: cfg["io"])
    force: bool = False  # ricalcola anche se la chiave e' in cache
    no_cache: bool = False  # bypassa lettura E scrittura della cache
    values: dict[str, ForwardValueOverride] | None = None  # pid -> {value, rank}


def _forward_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _forward_as_float(x: Any) -> float | None:
    """Conversione sicura a float finito: None su input non numerico/non finito."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _forward_state_hash(snapshot: dict[str, Any]) -> str:
    """Hash dell'input INDEPENDENTE da cfg/values: rappresenta lo stato
    corrente del motore (teams/players/pool). Cambia su acquisto/undo/
    correct/config e quindi invalida cache e 'latest' (content-addressed)."""
    payload = {
        "schema_version": snapshot.get("schema_version"),
        "snapshot": snapshot,
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


def _forward_base_snapshot() -> tuple[dict[str, Any], str, int]:
    """Snapshot read-only dello stato corrente (senza values) + state_hash +
    event_seq (azioni attive del motore). Nessuna mutazione."""
    snap = snapshot_from_auction(engine)
    return snap, _forward_state_hash(snap), len(engine.events)


def _forward_default_values(
    pool_pids: list[str], perspective: str
) -> dict[str, dict[str, Any]]:
    """Values di default (source 'my_team_value'): ranking condiviso dal blocco
    fantasy (score, fallback utility) e valore monetario dal blocco
    my_team.team_value della squadra perspective. MAI il prezzo di mercato:
    resta nel blocco 'market', separato per contratto WP7.

    Ordinamento deterministico: score discendente, tie-break pid crescente;
    rank 1..N. team_value None (squadra untracked) -> value None (input
    assente, gestito dal core come warning di fattibilita')."""
    scored: list[tuple[str, float, Any]] = []
    for pid in pool_pids:
        p = engine.players[pid]
        ev = engine.evaluate(p, perspective)
        fan = ev.get("fantasy") or {}
        mt = ev.get("my_team") or {}
        score = fan.get("score")
        if score is None:
            score = fan.get("utility")
        if score is None:
            score = -1.0  # nessun dato: ultima posizione, deterministica per pid
        score = _forward_as_float(score)
        scored.append((pid, score if score is not None else -1.0, mt.get("team_value")))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return {
        pid: {
            "value": _forward_as_float(tv),
            "rank": i,
        }
        for i, (pid, _, tv) in enumerate(scored, start=1)
    }


def _forward_resolve_values(
    body_values: dict[str, ForwardValueOverride] | None, perspective: str
) -> tuple[dict[str, dict[str, Any]], str]:
    """None -> ranking di default da fantasy.score + my_team.team_value
    (source 'my_team_value'); dict (anche vuoto) -> override esplicito che
    prevale (source 'override')."""
    pool_pids = sorted(
        pid for pid in engine.state["pool"] if engine.players[pid]["ruolo"] == "A"
    )
    if body_values is None:
        return _forward_default_values(pool_pids, perspective), (
            FORWARD_VALUE_SOURCE_DEFAULT
        )
    return (
        {pid: {"value": v.value, "rank": v.rank} for pid, v in body_values.items()},
        FORWARD_VALUE_SOURCE_OVERRIDE,
    )


@app.get("/api/forward/snapshot")
def api_forward_snapshot():
    """Snapshot JSON v1 read-only dello stato corrente per il simulatore
    Attaccanti: solo ruolo A nel pool, budget/slot A + slots_other per squadra.
    Espone state_hash/event_seq. Nessuna mutazione del motore."""
    snap, state_hash, event_seq = _forward_base_snapshot()
    return {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "read_only": True,
        "snapshot": snap,
        "state_hash": state_hash,
        "event_seq": event_seq,
        "pool_count": len(snap["pool"]),
        "teams_count": len(snap["teams"]),
        "generated_at": _forward_utc(),
    }


@app.post("/api/forward/simulate")
def api_forward_simulate(body: ForwardSimulateBody):
    """Simulazione Monte Carlo deterministica della fase A corrente.

    Body: runs (default 10000, bounded 1..50000), seed, player_order,
    team (perspective), force/no_cache, values override pid->{value,rank}.
    Cache content-addressed: key = snapshot(+values) + cfg. Un acquisto/
    undo/correct/config cambia lo snapshot hash e causa miss automatico.
    400 = input invalido; 409 = stato di simulazione infeasibile.
    La persistenza (store attivo o meno) non cambia la semantica."""
    if not (FORWARD_MIN_RUNS <= body.runs <= FORWARD_MAX_RUNS):
        return JSONResponse(
            {
                "error": f"runs deve essere in [{FORWARD_MIN_RUNS}, "
                f"{FORWARD_MAX_RUNS}] (trovato {body.runs})"
            },
            status_code=400,
        )
    if body.player_order not in ("shuffle", "by_value"):
        return JSONResponse(
            {"error": "player_order deve essere 'shuffle' o 'by_value'"},
            status_code=400,
        )
    try:
        perspective = engine._resolve_team(body.team) if body.team else engine.cfg["io"]
    except InvalidTeamError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    snap0, state_hash, event_seq = _forward_base_snapshot()
    if not snap0["pool"]:
        return JSONResponse(
            {"error": "pool Attaccanti vuoto: niente da simulare"},
            status_code=400,
        )
    values, values_source = _forward_resolve_values(body.values, perspective)
    if values_source == FORWARD_VALUE_SOURCE_OVERRIDE:
        unknown = sorted(set(values) - set(snap0["pool"]))
        if unknown:
            return JSONResponse(
                {
                    "error": "values: pid sconosciuti o non nel pool Attaccanti: "
                    + ", ".join(unknown)
                },
                status_code=400,
            )

    snap = snapshot_from_auction(engine, values=values)
    cfg = BidConfig(
        n_runs=body.runs,
        seed=body.seed,
        player_order=body.player_order,
    )
    errs = cfg.validate() + forward_validate_snapshot(snap, cfg)
    if errs:
        return JSONResponse(
            {"error": "input simulazione invalido: " + "; ".join(errs)},
            status_code=400,
        )
    key = forward_snapshot_key(snap, cfg)

    envelope = None
    if not body.no_cache and not body.force:
        with _FORWARD_LOCK:
            envelope = _FORWARD_CACHE.get(key)
    if envelope is not None:
        out = dict(envelope)
        out["cached"] = True
        # l'input e' identico (stesso snapshot+values+cfg), quindi il report e'
        # quello cache: esponiamo pero' i parametri della richiesta corrente
        out["simulate_params"] = {
            "runs": body.runs,
            "seed": body.seed,
            "player_order": body.player_order,
            "team": perspective,
        }
        out["values_source"] = values_source
        return out

    # miss: la simulazione gira FUORI dal lock (il lock protegge solo le mappe)
    t0 = time.perf_counter()
    sim_result = forward_simulate(snap, cfg)
    if isinstance(sim_result, tuple):  # trace=False: sempre SimResult, difensivo
        sim_result = sim_result[0]
    result = forward_report(sim_result, snap, cfg, deterministic_report=True)
    duration_ms = (time.perf_counter() - t0) * 1000
    feasibility = result.get("feasibility", {})
    if not feasibility.get("ok", False):
        return JSONResponse(
            {
                "error": "stato di simulazione infeasibile: "
                f"shortfall {feasibility.get('shortfall')}, squadre infeasibili: "
                f"{[t['team'] for t in feasibility.get('teams_infeasible', [])]}",
                "feasibility": feasibility,
                "state_hash": state_hash,
            },
            status_code=409,
        )

    envelope = {
        "cached": False,
        "duration_ms": round(duration_ms, 3),
        "state_hash": state_hash,
        "event_seq": event_seq,
        "generated_at": _forward_utc(),
        "values_source": values_source,
        "cache_key": key,
        "simulate_params": {
            "runs": body.runs,
            "seed": body.seed,
            "player_order": body.player_order,
            "team": perspective,
        },
        "result": result,
    }
    if not body.no_cache:
        with _FORWARD_LOCK:
            _FORWARD_CACHE[key] = envelope
            _FORWARD_LATEST[state_hash] = envelope
    return dict(envelope)


@app.get("/api/forward/latest")
def api_forward_latest():
    """Ultimo report di simulazione per lo stato CORRENTE dell'asta
    (content-addressed per state_hash): se l'asta e' cambiata dal calcolo
    (acquisto/undo/correct/config) non viene servito alcun report stale."""
    _, state_hash, event_seq = _forward_base_snapshot()
    with _FORWARD_LOCK:
        entry = _FORWARD_LATEST.get(state_hash)
    if entry is None or entry.get("state_hash") != state_hash:
        return JSONResponse(
            {
                "error": "nessun report di simulazione per lo stato corrente dell'asta",
                "state_hash": state_hash,
                "event_seq": event_seq,
            },
            status_code=404,
        )
    out = dict(entry)
    out["cached"] = True
    return out


# ------------------------------------------------------------------ persistenza
class RestoreBody(BaseModel):
    mode: str = "append"
    events: list[dict[str, Any]] | None = None
    meta: dict[str, Any] | None = None


class CorrectBody(BaseModel):
    target_seq: int | None = None
    key: str | None = None
    kind: str = "restate"
    price: int | None = None
    team: str | None = None


@app.get("/api/backup")
def api_backup():
    """Backup JSON versionato del log eventi (+ meta), unica fonte di verita'.
    In modalita' in-memory: 409 (niente log da scaricare)."""
    if store is None:
        return JSONResponse(
            {"error": "persistenza disattivata (--no-store): nessun backup"},
            status_code=409,
        )
    try:
        events = store.read_all()
        meta = store.read_all_meta()
    except StoreError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {
        "format": "openfanta-draft-events",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": meta,
        "persistence": _persistence_block(),
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


@app.post("/api/restore")
def api_restore(body: RestoreBody):
    """Restore del log: ``mode`` append|replace. Il risultato viene VALIDATO
    integralmente (store temporaneo + replay completo) PRIMA di sostituire lo
    stato: su errore DB e motore restano intatti (400/409 chiari)."""
    if store is None:
        return JSONResponse(
            {"error": "persistenza disattivata (--no-store)"}, status_code=409
        )
    if body.events is None:
        return JSONResponse({"error": "events richiesto"}, status_code=400)
    if body.mode not in ("append", "replace"):
        return JSONResponse(
            {"error": "mode deve essere 'append' o 'replace'"}, status_code=400
        )
    # validazione completa su temp/replay PRIMA di toccare il DB
    try:
        _validate_restore(body.events, body.meta, body.mode)
    except StoreError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        result = store.import_data(
            {
                "format": "openfanta-draft-events",
                "version": 1,
                "events": body.events,
                **({} if body.meta is None else {"meta": body.meta}),
            },
            mode=body.mode,
        )
        _rebuild_engine_from_store()
    except StoreError as e:
        _rebuild_engine_from_store()
        return JSONResponse({"error": str(e)}, status_code=409)
    return {
        "ok": True,
        **result,
        "nomination": engine.nomination_summary(),
        "persistence": _persistence_block(),
    }


@app.post("/api/correct")
def api_correct(body: CorrectBody):
    """Rettifica di un'azione attiva (sold/unsold non revocata): revoke+restate.

    ``target_seq` si riferisce a una ``seq`` del log; ``key`` a un giocatore
    (pid o nome) di cui si rettifica l'ultima azione attiva. ``kind="restate"``
    (default) appende un ``revoke`` del target ED un nuovo ``sold`` (prezzo/squadra
    corretti) in UN comando logico atomico; ``kind="revoke"`` solo il revoke.
    Il valore del restate e' validato su un motore temporaneo (replay senza il
    target + nuova vendita) PRIMA di appendere. 400 = input/validazione,
    409 = conflitto di stato (target gia' rettificato, invarianti)."""
    if store is None:
        return JSONResponse(
            {"error": "persistenza disattivata (--no-store)"}, status_code=409
        )
    if body.kind not in ("revoke", "restate"):
        return JSONResponse(
            {"error": "kind deve essere 'revoke' o 'restate'"}, status_code=400
        )
    events = store.read_all()
    superseded = {e["supersedes"] for e in events if e["supersedes"] is not None}
    # --- risoluzione del target ---
    if body.target_seq is not None:
        target = next((e for e in events if e["seq"] == body.target_seq), None)
        if target is None:
            return JSONResponse(
                {"error": f"evento {body.target_seq} non trovato nel log"},
                status_code=400,
            )
    else:
        if not body.key:
            return JSONResponse({"error": "serve target_seq o key"}, status_code=400)
        try:
            pid = engine.resolve(body.key)
        except AmbiguousName as e:
            return JSONResponse({"error": f"nome ambiguo: {e}"}, status_code=400)
        if pid is None:
            return JSONResponse({"error": "giocatore non trovato"}, status_code=404)
        target = next(
            (
                e
                for e in reversed(events)
                if e["type"] in ("sold", "unsold")
                and e.get("payload", {}).get("pid") == pid
            ),
            None,
        )
        if target is None:
            return JSONResponse(
                {"error": "nessuna azione attiva per il giocatore"}, status_code=400
            )
    if target["type"] not in ("sold", "unsold"):
        return JSONResponse(
            {
                "error": f"target {target['seq']} non e' una vendita/invenduto "
                "rettificabile"
            },
            status_code=400,
        )
    if target["seq"] in superseded:
        return JSONResponse(
            {"error": f"evento {target['seq']} gia' rettificato"}, status_code=409
        )
    tpid = target["payload"]["pid"]
    if body.kind == "revoke":
        try:
            store.append(
                "revoke",
                {"target_seq": target["seq"], "reason": "correct/revoke"},
                supersedes=target["seq"],
            )
            _rebuild_engine_from_store()
        except StoreError as e:
            _rebuild_engine_from_store()
            return JSONResponse({"error": str(e)}, status_code=409)
        return {
            "ok": True,
            "revoked": target["seq"],
            "nomination": engine.nomination_summary(),
            "persistence": _persistence_block(),
        }
    # --- restate (revoke + nuova sold atomica) ---
    if target["type"] != "sold":
        return JSONResponse(
            {"error": "restate richiede una vendita come target"}, status_code=400
        )
    if body.price is None or type(body.price) is not int or body.price < 1:
        return JSONResponse(
            {"error": "price deve essere un intero >= 1 per restate"}, status_code=400
        )
    base = [e for e in active_events(events) if e["seq"] != target["seq"]]
    try:
        probe = replay_events(base, PLAYERS, TrendAuction)
    except StoreError as e:
        return JSONResponse({"error": f"stato non ricostruibile: {e}"}, status_code=409)
    p = probe.players.get(tpid)
    if p is None:
        return JSONResponse(
            {"error": "giocatore del target assente dal listone (drift)"},
            status_code=409,
        )
    try:
        eval_before = probe.evaluate(p, body.team)
        team = probe.mark_sold(p, body.price, body.team, eval_before)
    except AuctionError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if probe.check_invariants():
        return JSONResponse(
            {"error": "il restate violerebbe le invarianti di dominio"}, status_code=409
        )
    try:
        store.append_batch(
            [
                (
                    "revoke",
                    {"target_seq": target["seq"], "reason": "correct/restate"},
                    target["seq"],
                ),
                (
                    "sold",
                    {
                        "pid": tpid,
                        "nome": p["nome"],
                        "ruolo": p["ruolo"],
                        "price": body.price,
                        "team": team,
                        "base": p["base"],
                    },
                    None,
                ),
            ]
        )
        _rebuild_engine_from_store()
    except StoreError as e:
        _rebuild_engine_from_store()
        return JSONResponse({"error": str(e)}, status_code=409)
    return {
        "ok": True,
        "corrected": target["seq"],
        "new": {"price": body.price, "team": team},
        "nomination": engine.nomination_summary(),
        "persistence": _persistence_block(),
    }


# ------------------------------------------------------------------- export
def csv_response(rows, fieldnames, filename):
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM per Excel
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}_{stamp}.csv"},
    )


def svincolati_rows(ruolo: str = "", q: str = "", team: str | None = None):
    query = norm(q)
    rows = []
    for key in engine.state["pool"]:
        p = engine.players[key]
        if ruolo and p["ruolo"] != ruolo:
            continue
        if query and query not in norm(p["nome"]) and query not in norm(p["squadra"]):
            continue
        evaluation = engine.evaluate(p, team)
        rows.append(
            {
                "pid": p["pid"],
                "role": p["ruolo"],
                "name": p["nome"],
                "club": p["squadra"],
                "tier": p["fascia"],
                "status": p["status"],
                "slot": p["slot"],
                "pfc": p["base"],
                "pfc_range": p["pfc_range"],
                "pma": round(p["pma"], 1),
                "pma_range": p["pma_range"],
                "pfc_vs_pma": round(p["dpfcpma"], 1),
                "starter_pct": p["tit"],
                "tix": p["tix"],
                "expected_fm": p["expfm"],
                "fix": p["fix"],
                "daily_contribution": p["fix_contrib"],
                "scarcity": round(evaluation["scarc"], 2),
                "alternative_value": evaluation["alt_value"],
                "suggested_price": evaluation["suggested"],
                "unsold_count": engine.state["unsold"].get(key, 0),
            }
        )
    role_index = {role: i for i, role in enumerate(ROLE_ORDER)}
    rows.sort(key=lambda row: (role_index[row["role"]], -row["pfc"], row["name"]))
    return rows


def _valid_role(ruolo: str):
    role = ruolo.strip().upper()
    if role and role not in ROLE_ORDER:
        return None
    return role


@app.get("/api/svincolati")
def api_svincolati(ruolo: str = "", q: str = "", team: str | None = None):
    role = _valid_role(ruolo)
    if role is None:
        return JSONResponse(
            {"error": "ruolo non valido (P, D, C, A o vuoto)"}, status_code=400
        )
    rows = svincolati_rows(role, q, team)
    return {"rows": rows, "count": len(rows), "role": role, "query": q.strip()}


@app.get("/api/export/svincolati")
def export_svincolati(ruolo: str = ""):
    """Lista dei svincolati (ancora in carta) con valutazione live, filtrabile per ruolo."""
    role = _valid_role(ruolo)
    if role is None:
        return JSONResponse(
            {"error": "ruolo non valido (P, D, C, A o vuoto)"}, status_code=400
        )
    source = svincolati_rows(role)
    rows = [
        {
            "Pid": row["pid"],
            "Ruolo": row["role"],
            "Nome": row["name"],
            "Squadra": row["club"],
            "Fascia": row["tier"],
            "Status": row["status"],
            "Slot consigliato": row["slot"],
            "PFC": row["pfc"],
            "Range PFC": row["pfc_range"],
            "PMA": row["pma"],
            "Range PMA": row["pma_range"],
            "PFC vs PMA": row["pfc_vs_pma"],
            "Titolarita%": row["starter_pct"],
            "TIX": row["tix"],
            "FM attesa": row["expected_fm"],
            "FIX": row["fix"],
            "Contributo/gior": row["daily_contribution"],
            "Scarsita": row["scarcity"],
            "Valore alternative": row["alternative_value"],
            "Prezzo suggerito": row["suggested_price"],
            "Invenduto (volte)": row["unsold_count"],
        }
        for row in source
    ]
    fields = (
        list(rows[0].keys())
        if rows
        else [
            "Pid",
            "Ruolo",
            "Nome",
            "Squadra",
            "Fascia",
            "Status",
            "Slot consigliato",
            "PFC",
            "Range PFC",
            "PMA",
            "Range PMA",
            "PFC vs PMA",
            "Titolarita%",
            "TIX",
            "FM attesa",
            "FIX",
            "Contributo/gior",
            "Scarsita",
            "Valore alternative",
            "Prezzo suggerito",
            "Invenduto (volte)",
        ]
    )
    name = f"svincolati_{role.lower() if role else 'tutti'}"
    return csv_response(rows, fields, name)


def rose_rows(team: str = "", ruolo: str = ""):
    rows = []
    for ev in engine.events:
        if ev["kind"] != "sold":
            continue
        if team and ev["team"] != team:
            continue
        if ruolo and ev["ruolo"] != ruolo:
            continue
        p = engine.players.get(ev.get("pid"))
        pma = p["pma"] if p else None
        rows.append(
            {
                "pid": ev.get("pid"),
                "team": ev["team"],
                "role": ev["ruolo"],
                "player": ev["nome"],
                "price": ev["price"],
                "pfc": ev["base"],
                "premium_pfc_pct": round(100 * (ev["price"] / ev["base"] - 1), 1),
                "pma": round(pma, 1) if pma else None,
                "premium_pma_pct": round(100 * (ev["price"] / pma - 1), 1)
                if pma
                else None,
                "purchase_number": ev["i"] + 1,
            }
        )
    role_index = {role: i for i, role in enumerate(ROLE_ORDER)}
    rows.sort(key=lambda row: (row["team"], role_index[row["role"]], -row["price"]))
    return rows


def rose_summaries():
    sold = rose_rows()
    summaries = []
    for team in engine.cfg["team_names"]:
        purchases = [row for row in sold if row["team"] == team]
        filled = dict.fromkeys(ROLE_ORDER, 0)
        for row in purchases:
            filled[row["role"]] += 1
        summaries.append(
            {
                "team": team,
                "spent": sum(row["price"] for row in purchases),
                "remaining_budget": engine.state["money"][team],
                "purchases": len(purchases),
                "filled_slots": filled,
                "total_slots": dict(engine.cfg["slots"]),
            }
        )
    other = [row for row in sold if row["team"] == "ALTRO"]
    if other:
        summaries.append(
            {
                "team": "ALTRO",
                "spent": sum(row["price"] for row in other),
                "remaining_budget": None,
                "purchases": len(other),
                "filled_slots": None,
                "total_slots": None,
            }
        )
    return summaries


@app.get("/api/rose")
def api_rose(team: str = "", ruolo: str = ""):
    role = _valid_role(ruolo)
    if role is None:
        return JSONResponse(
            {"error": "ruolo non valido (P, D, C, A o vuoto)"}, status_code=400
        )
    valid_teams = [*engine.cfg["team_names"], "ALTRO"]
    if team and team not in valid_teams:
        return JSONResponse({"error": "squadra non valida"}, status_code=400)
    rows = rose_rows(team, role)
    return {
        "rows": rows,
        "count": len(rows),
        "team": team,
        "role": role,
        "teams": valid_teams,
        "summaries": rose_summaries(),
    }


def _engine_without_purchase(target: dict[str, Any]) -> TrendAuction:
    """Ricostruisce lo stato in-memory omettendo una vendita.

    Gli altri eventi restano nello stesso ordine, inclusi eventuali ``unsold``
    precedenti. Gli snapshot di undo vengono rigenerati durante il replay, per
    cui l'undo in-memory continua a riferirsi all'ultima azione rimasta.
    """
    source_players = PLAYERS or [dict(player) for player in engine.players.values()]
    rebuilt = TrendAuction(source_players, **dict(engine.cfg))
    skipped = False
    for event in engine.events:
        if event is target:
            skipped = True
            continue
        player = rebuilt.players[event["pid"]]
        if event["kind"] == "sold":
            evaluation = rebuilt.evaluate(player, event.get("team"))
            rebuilt.mark_sold(player, event["price"], event.get("team"), evaluation)
        else:
            rebuilt.mark_unsold(player)
    if not skipped:
        raise AuctionError("acquisto da rimuovere non trovato")
    return rebuilt


@app.delete("/api/rose/{pid}")
def api_rose_delete(pid: str):
    """Rimuove un acquisto dalla rosa e restituisce budget/slot alla squadra."""
    global engine
    target = next(
        (
            event
            for event in reversed(engine.events)
            if event["kind"] == "sold" and event["pid"] == pid
        ),
        None,
    )
    if target is None:
        return JSONResponse(
            {"error": "acquisto non trovato nella rosa"}, status_code=404
        )
    if store is not None:
        result = api_correct(CorrectBody(key=pid, kind="revoke"))
        if isinstance(result, JSONResponse):
            return result
    else:
        try:
            engine = _engine_without_purchase(target)
        except (AuctionError, ConfigError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        result = {
            "ok": True,
            "nomination": engine.nomination_summary(),
            "persistence": _persistence_block(),
        }
    return {
        **result,
        "removed": {
            "pid": pid,
            "player": target["nome"],
            "team": target["team"],
            "price": target["price"],
        },
    }


@app.get("/api/export/rose")
def export_rose():
    """Rose delle squadre allo stato attuale: chi ha comprato cosa e a quale prezzo."""
    rows = [
        {
            "Pid": row["pid"],
            "Squadra": row["team"],
            "Ruolo": row["role"],
            "Giocatore": row["player"],
            "Prezzo": row["price"],
            "PFC": row["pfc"],
            "Premium vs PFC%": row["premium_pfc_pct"],
            "PMA": row["pma"],
            "Premium vs PMA%": row["premium_pma_pct"],
            "N. acquisto": row["purchase_number"],
        }
        for row in rose_rows()
    ]
    return csv_response(
        rows,
        [
            "Pid",
            "Squadra",
            "Ruolo",
            "Giocatore",
            "Prezzo",
            "PFC",
            "Premium vs PFC%",
            "PMA",
            "Premium vs PMA%",
            "N. acquisto",
        ],
        "rose_squadre",
    )


def main():
    global engine, PLAYERS, store
    ap = argparse.ArgumentParser(
        description="GUI asta live Fantacalcio 2026/27 (Listone Fantaculo)"
    )
    ap.add_argument("--csv", default=DEFAULT_CSV, help="CSV canonico del listone")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--squadre", type=int, default=DEFAULTS["teams"])
    ap.add_argument("--budget", type=int, default=DEFAULTS["budget"])
    ap.add_argument("--io", default=DEFAULTS["io"])
    ap.add_argument(
        "--store",
        default=DEFAULT_STORE_DB,
        help=f"percorso del DB event-sourced (default: {DEFAULT_STORE_DB})",
    )
    ap.add_argument(
        "--no-store",
        action="store_true",
        help="modalita' in-memory: lo stato NON viene persistito (solo undo snapshot)",
    )
    args = ap.parse_args()
    PLAYERS = load_players(args.csv)
    cfg = league_config.normalize(
        dict(DEFAULTS, teams=args.squadre, budget=args.budget, io=args.io)
    )
    try:
        TrendAuction.validate_config(PLAYERS, cfg)
    except ConfigError as e:
        print(f"Configurazione non valida: {e}", file=sys.stderr)
        sys.exit(1)
    if args.no_store:
        store = None
        engine = TrendAuction(
            PLAYERS, teams=args.squadre, budget=args.budget, io=args.io
        )
    else:
        st = AuctionStore(args.store)
        store = st
        if st.latest() is None:
            # DB vuoto: il primo league_configured dal CLI. Config + lista validata
            # sopra; il replay da qui dara' lo stesso motore del --demo.
            st.append("league_configured", {"config": dict(cfg)})
            st.set_meta("league_cfg", dict(cfg))
            engine = replay_engine(st, PLAYERS, TrendAuction)
        else:
            # resume deterministico: ricostruisce config+stato dal log. Su errore
            # (listone cambiato / config non sostenibile) esce con messaggio chiaro.
            try:
                engine = replay_engine(st, PLAYERS, TrendAuction)
            except StoreError as e:
                print(f"Resume fallito: {e}", file=sys.stderr)
                sys.exit(1)
            removed = engine.replay_drift_removed
            if removed:
                names = ", ".join(
                    str(item.get("nome") or item["pid"]) for item in removed
                )
                print(
                    "Resume completato: rimossi dalle rose i giocatori non piu' "
                    f"nel listone ({names}); azioni revocate nel log.",
                    file=sys.stderr,
                )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
