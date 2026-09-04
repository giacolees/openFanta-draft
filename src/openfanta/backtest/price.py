#!/usr/bin/env -S uv run --quiet python
"""Backtest del prezzo d'asta in replay PREQUENZIALE (WP9).

Ripete lo storico delle vendite **in ordine** e, per ogni vendita, predice il
prezzo PRIMA di applicarla: nessun evento futuro (mai la vendita stessa, mai
quelle successive) e' usato nella predizione. Al termine confronta i modelli
e valuta il gate di qualita' (``openfanta.core.gates``).

Modelli confrontati (le baseline sono SEMPRE presenti nel report):

- ``pfc``         — PFC puro del listone;
- ``pma``         — PMA (prezzi medi d'Italia) del listone;
- ``live``        — formula live ``PFC x inflazione x scarsita' x sconto``
                    (identica a ``suggested``, calcolata sullo stato del
                    replay: la scarsita'/inflazione evolvono a ogni vendita);
- ``calibrated``  — la formula live moltiplicata per il fattore advisory di
                    calibrazione (WP8) addestrato SOLO sulle vendite
                    precedenti (advisory-only: non attiva mai nulla).

Input ``--sales`` (due formati, riconosciuti dal contenuto):

- **CSV storico** con schema ``pid,nome,ruolo,price,team,seq,ts,season``:
  ``pid`` preferito (nome accettato come legacy se risolve un giocatore
  unico del listone); una riga = una vendita;
- **backup JSON dell'event store** (``format=openfanta-draft-events``,
  prodotto da ``GET /api/backup`` o ``AuctionStore.backup``): sono usati gli
  eventi attivi (esclusi i revocati via ``supersedes``) in ordine di ``seq``;
  gli eventi ``unsold`` sono ri-applicati nel replay (influenzano lo sconto).

Errori bloccanti (exit 2, nessun artefatto scritto): file mancanti,
giocatore assente dal listone, prezzo non intero positivo, vendita duplicata,
ruolo incoerente con il listone, squadra tracciata oltre budget. Una squadra
NON presente nella config diventa ``ALTRO`` esplicito (invarianti budget
rispettate: ALTRO e' la semantica del motore).

Output atomici (temp + rename): ``<out>/backtest_auction_report.json`` (report
completo, incluso ``gate_recommendation``), ``.csv`` (metriche per modello e
slice), ``.txt`` (sintesi umana). Nessun timestamp nel contenuto: output
deterministico. Exit 0 = report scritti; exit 2 = input mancante/invalido o
nessun dato.

Il gate produce un ``gate_recommendation``: NON attiva nulla. Il flag
``use_calibration_in_price`` resta False finche' una decisione esplicita
futura non lo consente (ADR: calibrazione/modello fantasy fuori dal prezzo
finche' il gate out-of-sample non superato).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from openfanta.core import calibration, gates, valuation
from openfanta.core.auction import Auction, AuctionError, ConfigError, load_players
from openfanta.core.config import norm, normalize, validate

DEFAULT_LISTONE = "data/listone.csv"
DEFAULT_OUT_DIR = "data"
DEFAULT_REPORT_BASE = "backtest_auction_report"
BACKUP_FORMAT = "openfanta-draft-events"

MODELS = ("pfc", "pma", "live", "calibrated")
BASELINES = ("pfc", "pma", "live")
_SLICE_TYPES = ("global", "role", "band", "phase")


class SalesError(Exception):
    """Errore di input nel file vendite (riga contestualizzata, bloccante)."""


# ---------------------------------------------------------------------------
# eventi di vendita: modello comune a CSV e backup
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuctionEvent:
    """Evento di asta normalizzato per il replay (vendita o invenduto)."""

    kind: str  # "sold" | "unsold"
    pid: str
    nome: str
    ruolo: str
    price: int | None  # sold only
    team: str  # canonico: squadra tracciata o "ALTRO"
    seq: int | None
    ts: str | None
    season: str
    base: float | None  # dal payload del backup (se presente)


def _r4(x: float | None) -> float | None:
    if x is None:
        return None
    if not math.isfinite(x):
        return None
    return round(x, 4)


# ---------------------------------------------------------------------------
# parsing dei formati di vendita
# ---------------------------------------------------------------------------
def _looks_like_json(path: str) -> bool:
    if path.lower().endswith(".json"):
        return True
    try:
        with open(path, encoding="utf-8-sig") as f:
            head = f.read(64).lstrip()
    except OSError as e:
        raise SalesError(f"file vendite non leggibile: {path} ({e})") from e
    return head.startswith("{")


def _norm_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _parse_seq(raw: Any, where: str) -> int | None:
    text = _norm_str(raw)
    if not text:
        return None
    try:
        seq = int(text)
    except ValueError as e:
        raise SalesError(f"{where}: seq non valido (atteso intero): {text!r}") from e
    if seq < 1:
        raise SalesError(f"{where}: seq deve essere >= 1 (trovato {seq})")
    return seq


def _parse_price(raw: Any, where: str, kind: str) -> int | None:
    if kind == "unsold":
        return None
    text = _norm_str(raw)
    if not text:
        raise SalesError(f"{where}: prezzo mancante per una vendita")
    try:
        price = int(text)
    except ValueError as e:
        raise SalesError(
            f"{where}: prezzo non valido (atteso intero >= 1): {text!r}"
        ) from e
    if price < 1:
        raise SalesError(f"{where}: prezzo deve essere >= 1 (trovato {price})")
    return price


def _resolve_player(
    pid_raw: str,
    nome: str,
    ruolo: str,
    by_pid: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    where: str,
) -> dict[str, Any]:
    """Risoluzione del giocatore: pid preferito, nome legacy come fallback.

    Il nome e' accettato solo se risolve UN giocatore (eventualmente con
    l'aiuto del ruolo). Errori chiari per giocatore assente o ambiguo."""
    pid = pid_raw.strip()
    if pid:
        p = by_pid.get(pid)
        if p is None:
            raise SalesError(f"{where}: giocatore assente dal listone (pid={pid!r})")
        return p
    nome_key = norm(nome)
    if not nome_key:
        raise SalesError(
            f"{where}: riga senza pid ne' nome: giocatore non identificabile"
        )
    candidates = by_name.get(nome_key, [])
    if ruolo:
        candidates = [c for c in candidates if c["ruolo"] == ruolo]
    if not candidates:
        raise SalesError(f"{where}: giocatore assente dal listone (nome={nome!r})")
    if len(candidates) > 1:
        raise SalesError(
            f"{where}: nome ambiguo nel listone (nome={nome!r}): {len(candidates)} "
            "giocatori; servono pid o ruolo per disambiguare"
        )
    return candidates[0]


def _canonical_team(team: str, team_names: Iterable[str]) -> str:
    """Squadra canonica: nome tracciato (match normalizzato) o ALTRO esplicito.

    Una squadra NON nella config della lega non puo' essere simulata: diventa
    ALTRO (l'acquirente non tracciato della semantica del motore), cosi' le
    invarianti di budget restano rispettate."""
    key = norm(team) if team else ""
    if key:
        for name in team_names:
            if norm(name) == key:
                return name
    return "ALTRO"


def _derived_base(p: dict[str, Any]) -> int:
    """Base del giocatore calcolata con la STESSA formula del motore
    (``max(1, round(pfc))``): il listone caricato da ``load_players`` non
    ha ancora la chiave ``base`` (la aggiunge solo il costruttore del motore
    sulle sue copie interne)."""
    return max(1, round(p["pfc"]))


def _base_from_payload(payload: dict[str, Any], p: dict[str, Any]) -> int:
    """Base del replay: preferita la base registrata nel payload del backup
    (intero positivo); altrimenti derivata dal pfc del listone con la STESSA
    formula del motore."""
    raw = payload.get("base")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    if isinstance(raw, float) and math.isfinite(raw) and raw > 0:
        return max(1, round(raw))
    return _derived_base(p)


def sort_sales(events: list[AuctionEvent]) -> list[AuctionEvent]:
    """Ordine deterministico del replay: (stagione, seq|ts, posizione file).

    La chiave dentro la stagione e' ``seq`` se TUTTI gli eventi lo hanno,
    altrimenti ``ts`` se TUTTI lo hanno, altrimenti l'ordine di lettura.
    Stabile: mai riordino arbitrario a parita' di chiave."""
    if not events:
        return []
    indexed = list(enumerate(events))
    if all(ev.seq is not None for _, ev in indexed):

        def key(ie: tuple[int, AuctionEvent]) -> tuple[Any, ...]:
            return (ie[1].season, ie[1].seq or 0, ie[0])

    elif all(ev.ts for _, ev in indexed):

        def key(ie: tuple[int, AuctionEvent]) -> tuple[Any, ...]:
            return (ie[1].season, ie[1].ts or "", ie[0])

    else:

        def key(ie: tuple[int, AuctionEvent]) -> tuple[Any, ...]:
            return (ie[1].season, ie[0])

    return [ev for _, ev in sorted(indexed, key=key)]


def _season_from_ts(ts: str) -> str:
    """Stagione dedotta dal timestamp ISO (es. 2026-27: il campionato comincia
    ad agosto). Sola euristica di ordine/join: mai dati inventati."""
    year = ts[:4]
    if len(ts) >= 7 and ts[5] == "-" and ts[5:7].isdigit():
        try:
            year_num = int(year)
            month = int(ts[5:7])
        except ValueError:
            return year
        if month >= 7:
            return f"{year_num}-{str(year_num + 1)[2:]}"
        return f"{year_num - 1}-{year[2:]}"
    return year


# ---------------------------------------------------------------------------
# parsing dei formati di vendita
# ---------------------------------------------------------------------------
def parse_sales_csv(
    path: str,
    by_pid: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    team_names: Iterable[str],
) -> list[AuctionEvent]:
    """CSV storico delle vendite -> eventi normalizzati (errori contestualizzati)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError as e:
        raise FileNotFoundError(f"file vendite non trovato: {path}") from e
    events: list[AuctionEvent] = []
    for i, row in enumerate(rows):
        where = f"riga {i + 2} del CSV vendite"
        pid = _norm_str(row.get("pid"))
        nome = _norm_str(row.get("nome"))
        ruolo = _norm_str(row.get("ruolo")).upper()
        price = _parse_price(row.get("price"), where, "sold")
        seq = _parse_seq(row.get("seq"), where)
        ts = _norm_str(row.get("ts")) or None
        season = _norm_str(row.get("season"))
        p = _resolve_player(pid, nome, ruolo, by_pid, by_name, where)
        if ruolo and ruolo != p["ruolo"]:
            raise SalesError(
                f"{where}: ruolo {ruolo!r} incoerente con il listone "
                f"({p['nome']} e' {p['ruolo']})"
            )
        events.append(
            AuctionEvent(
                kind="sold",
                pid=p["pid"],
                nome=p["nome"],
                ruolo=p["ruolo"],
                price=price,
                team=_canonical_team(_norm_str(row.get("team")), team_names),
                seq=seq,
                ts=ts,
                season=season,
                base=_derived_base(p),
            )
        )
    return events


def parse_sales_backup(
    path: str,
    by_pid: dict[str, dict[str, Any]],
    team_names: Iterable[str],
) -> list[AuctionEvent]:
    """Backup JSON dell'event store -> eventi attivi (revoke/supersedes filtrati)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            doc = json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"file vendite non trovato: {path}") from e
    except json.JSONDecodeError as e:
        raise SalesError(f"backup JSON malformato: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        raise SalesError(
            "backup non riconosciuto: atteso un oggetto JSON con 'events' (lista)"
        )
    fmt = doc.get("format")
    if fmt is not None and fmt != BACKUP_FORMAT:
        raise SalesError(f"format del backup non riconosciuto: {fmt!r}")

    # eventi attivi: scarta i revocati (la loro seq compare in un supersedes)
    superseded: set[int] = set()
    for ev in doc["events"]:
        if not isinstance(ev, dict):
            raise SalesError("evento non valido nel backup (atteso un oggetto)")
        sup = ev.get("supersedes")
        if sup is not None:
            if isinstance(sup, int) and not isinstance(sup, bool) and sup > 0:
                superseded.add(sup)
            else:
                raise SalesError(
                    f"supersedes non valido nel backup (atteso intero >= 1): {sup!r}"
                )
    events: list[AuctionEvent] = []
    for i, ev in enumerate(doc["events"]):
        if ev.get("type") not in ("sold", "unsold"):
            continue  # league_configured/revoke: config da CLI, revoca e' un marker
        seq_raw = ev.get("seq")
        if not isinstance(seq_raw, int) or seq_raw < 1:
            raise SalesError(f"evento {i}: seq mancante o invalida nel backup")
        if seq_raw in superseded:
            continue  # revocato: non e' parte dello storico attivo
        kind = ev["type"]
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            raise SalesError(f"evento {seq_raw}: payload non valido nel backup")
        where = f"evento {seq_raw} del backup"
        pid = _norm_str(payload.get("pid"))
        if not pid or pid not in by_pid:
            raise SalesError(f"{where}: giocatore assente dal listone (pid={pid!r})")
        p = by_pid[pid]
        ruolo = _norm_str(payload.get("ruolo")).upper()
        if ruolo and ruolo != p["ruolo"]:
            raise SalesError(
                f"{where}: ruolo {ruolo!r} incoerente con il listone "
                f"({p['nome']} e' {p['ruolo']})"
            )
        ruolo = ruolo or p["ruolo"]
        price = _parse_price(payload.get("price"), where, kind)
        ts = _norm_str(ev.get("ts")) or None
        events.append(
            AuctionEvent(
                kind=kind,
                pid=pid,
                nome=_norm_str(payload.get("nome")) or p["nome"],
                ruolo=ruolo,
                price=price,
                team=_canonical_team(_norm_str(payload.get("team")), team_names),
                seq=seq_raw,
                ts=ts,
                season=_norm_str(payload.get("season")),
                base=_base_from_payload(payload, p),
            )
        )
    return events


def parse_sales(
    path: str,
    by_pid: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    team_names: Iterable[str],
) -> list[AuctionEvent]:
    """Carica le vendite nel formato giusto (JSON backup o CSV storico)."""
    if _looks_like_json(path):
        return parse_sales_backup(path, by_pid, team_names)
    return parse_sales_csv(path, by_pid, by_name, team_names)


def check_duplicates(events: list[AuctionEvent]) -> None:
    """Nessuna doppia vendita (sul replay attivo: le revocazioni sono gia'
    state filtrate dal parser del backup, quindi una ri-vendita dopo revoke
    resta legittima)."""
    sold: set[str] = set()
    for ev in events:
        if ev.kind != "sold":
            continue
        if ev.pid in sold:
            raise SalesError(
                f"vendita duplicata: {ev.nome} (pid={ev.pid}) compare piu' di una "
                "volta tra le vendite attive"
            )
        sold.add(ev.pid)


# ---------------------------------------------------------------------------
# replay prequenziale
# ---------------------------------------------------------------------------
def _phase_of(
    n_prior: int,
    role: str,
    cal_cfg: calibration.CalibrationConfig,
    league_cfg: dict[str, Any],
) -> str:
    """Fase dell'asta alla vendita n_prior+1 (stessa definizione di WP8)."""
    if cal_cfg.phase_mode == "role":
        denom = calibration._role_slots(league_cfg, role)
    else:
        denom = calibration.total_slots(league_cfg)
    if denom > 0:
        progress = (n_prior + 1) / denom
    else:
        # fallback documentato in WP8: senza slot derivabili si usa la
        # posizione relativa nella sequenza osservata
        progress = (n_prior + 1) / (n_prior + 1)
    return calibration._phase_of(progress, cal_cfg)


def run_replay(
    auction: Auction,
    events: list[AuctionEvent],
    cal_cfg: calibration.CalibrationConfig,
    league_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replay PREQUENZIALE: per ogni vendita predice PRIMA e applica DOPO.

    La calibrazione e' addestrata SOLO sulle vendite precedenti (prior): la
    predizione della vendita i non puo' contenere alcuna informazione della
    vendita i stessa o delle successive (no leakage futuro)."""
    predictions: list[dict[str, Any]] = []
    prior: list[dict[str, Any]] = []  # vendite attive gia' applicate (addestramento)
    for ev in events:
        p = auction.players.get(ev.pid)
        if p is None:
            raise SalesError(
                f"evento (pid={ev.pid}, {ev.nome}): giocatore non nel motore"
            )
        if ev.kind == "sold":
            if ev.price is None:
                raise SalesError(
                    f"evento incoerente: vendita senza prezzo (pid={ev.pid})"
                )
            seq_str = str(ev.seq) if ev.seq is not None else "-"
            phase = _phase_of(len(prior), ev.ruolo, cal_cfg, league_cfg)
            preds: dict[str, float] = {
                "pfc": p["base"],
                "pma": max(1, round(p["pma"])) if p.get("pma") else p["base"],
                "live": valuation.live_price(auction, p),
            }
            # advisory SOLO sulle vendite precedenti: report ricostruito a ogni
            # passo -> la vendita corrente non entra mai nel modello
            report = calibration.calibration_from_events(
                prior, league_cfg, config=cal_cfg
            )
            adv = calibration.estimate_player(report, p, current_phase=phase)
            preds["calibrated"] = max(1, round(preds["live"] * adv["factor"]))
            predictions.append(
                {
                    "seq": ev.seq,
                    "ts": ev.ts,
                    "season": ev.season,
                    "pid": ev.pid,
                    "nome": ev.nome,
                    "ruolo": ev.ruolo,
                    "band": calibration.band_for_base(p["base"], cal_cfg),
                    "phase": phase,
                    "team": ev.team,
                    "actual": ev.price,
                    "preds": preds,
                    "calibration": {
                        "factor": adv["factor"],
                        "source": adv["source"],
                        "n": adv["n"],
                    },
                }
            )
            try:
                auction.mark_sold(p, ev.price, ev.team)
            except AuctionError as e:
                raise SalesError(
                    f"vendita di {ev.nome} (seq={seq_str}) viola le "
                    f"invarianti di budget/slot del motore: {e}"
                ) from e
            base = p["base"]
            prior.append(
                {
                    "type": "sold",
                    "seq": len(prior) + 1,
                    "payload": {
                        "pid": ev.pid,
                        "nome": ev.nome,
                        "ruolo": ev.ruolo,
                        "price": ev.price,
                        "team": ev.team,
                        "base": base,
                        "premium_pfc": ev.price / base,
                    },
                }
            )
        else:  # unsold: cambia lo stato (sconto invenduto), nessuna predizione
            seq_str = str(ev.seq) if ev.seq is not None else "-"
            try:
                auction.mark_unsold(p)
            except AuctionError as e:
                raise SalesError(
                    f"invenduto di {ev.nome} (seq={seq_str}) invalido nel replay: {e}"
                ) from e
    return predictions


# ---------------------------------------------------------------------------
# metriche e gate
# ---------------------------------------------------------------------------
def _model_metrics(preds: list[dict[str, Any]], model: str) -> dict[str, Any]:
    actuals = [row["actual"] for row in preds]
    yhat = [row["preds"][model] for row in preds]
    return {
        "n": len(preds),
        "mae": _r4(gates.mae(actuals, yhat)),
        "rmse": _r4(gates.rmse(actuals, yhat)),
        "mape": _r4(gates.mape(actuals, yhat)),
        "spearman": _r4(gates.spearman(actuals, yhat)),
        "coverage": _r4(gates.coverage(actuals, yhat)),
    }


def slice_metrics(predictions: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Metriche per modello: global + per ruolo + per banda + per fase.

    Ogni slice esiste solo se ha almeno un'osservazione; metriche non
    computabili (es. Spearman con n<2) sono esplicitamente null."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for slice_type, field_name in (
        ("global", None),
        ("role", "ruolo"),
        ("band", "band"),
        ("phase", "phase"),
    ):
        out[slice_type] = {}
        if field_name is None:
            out[slice_type][""] = _model_metrics(predictions, model)
            continue
        slices: dict[str, list[dict[str, Any]]] = {}
        for row in predictions:
            slices.setdefault(str(row[field_name]), []).append(row)
        for value in sorted(slices):
            out[slice_type][value] = _model_metrics(slices[value], model)
    return out


def _global_metrics(models: dict[str, Any], model: str) -> dict[str, Any]:
    """Metriche globali del modello (chiave slice vuota nella struttura)."""
    return models[model]["global"][""]


def best_baseline_mape(models: dict[str, Any]) -> tuple[str | None, float | None]:
    """La MIGLIORE baseline per MAPE globale (ignorando MAPE non validi)."""
    candidates: list[tuple[str, float]] = []
    for name in BASELINES:
        m = _global_metrics(models, name)["mape"]
        if isinstance(m, (int, float)) and math.isfinite(m) and m > 0:
            candidates.append((name, m))
    if not candidates:
        return None, None
    return min(candidates, key=lambda t: t[1])


def evaluate_auction_gate(
    predictions: list[dict[str, Any]],
    models: dict[str, Any],
    gate_cfg: gates.GateConfig,
) -> dict[str, Any]:
    """Gate di calibrazione vs MIGLIORE baseline (PFC/PMA/live), out-of-sample.

    Mai pass con: n insufficiente, metriche NaN/assenti, leakage. Il verdetto
    e' solo una raccomandazione: NON attiva la calibrazione nel prezzo."""
    cal_global = _global_metrics(models, "calibrated")
    best_name, best_mape = best_baseline_mape(models)
    leakage = False  # il replay prequenziale per costruzione non ha leakage
    verdict = gates.evaluate_gate(
        n=len(predictions),
        mape_model=cal_global["mape"],
        mape_baseline=best_mape,
        spearman=cal_global["spearman"],
        leakage=leakage,
        config=gate_cfg,
    )
    return {
        "verdict": verdict.to_dict(),
        "best_baseline": best_name,
        "best_baseline_mape": _r4(best_mape),
        "use_calibration_in_price": False,
        "note": (
            "il gate NON attiva nulla: use_calibration_in_price resta False "
            "finche' una decisione esplicita futura non lo consente"
        ),
    }


# ---------------------------------------------------------------------------
# report atomici
# ---------------------------------------------------------------------------
def write_atomic(path: str, text: str) -> str:
    """Scrittura atomica (temp + rename): mai un report parziale su disco."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise SalesError(f"directory di output non creabile: {d}: {e}") from e
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".backtest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def report_paths(out: str) -> tuple[str, str, str]:
    """Percorsi dei tre report: `--out` = directory o base path (senza .json)."""
    if out.lower().endswith(".json"):
        base = out[:-5]
    else:
        base = os.path.join(out, DEFAULT_REPORT_BASE)
    return base + ".json", base + ".csv", base + ".txt"


def build_json_report(
    meta: dict[str, Any],
    models: dict[str, Any],
    gate: dict[str, Any],
    predictions: list[dict[str, Any]],
) -> str:
    doc = {
        "meta": meta,
        "models": models,
        "gate_recommendation": gate,
        "predictions": predictions,
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_csv_report(models: dict[str, Any]) -> str:
    """CSV con una riga per (modello, tipo di slice, slice)."""
    lines = ["model,slice_type,slice,n,mae,rmse,mape,spearman,coverage"]
    for model in MODELS:
        for slice_type in _SLICE_TYPES:
            slices = models[model][slice_type]
            for value in sorted(slices):
                m = slices[value]
                lines.append(
                    ",".join(
                        _csv_cell(v)
                        for v in (
                            model,
                            slice_type,
                            value,
                            m["n"],
                            m["mae"],
                            m["rmse"],
                            m["mape"],
                            m["spearman"],
                            m["coverage"],
                        )
                    )
                )
    return "\n".join(lines) + "\n"


def build_txt_report(
    meta: dict[str, Any],
    models: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    """Sintesi umana: gate, verdict, motivi e confronto baseline (globale)."""
    verdict = gate["verdict"]
    lines: list[str] = [
        "Backtest prezzo d'asta (WP9) — replay prequenziale",
        "=" * 50,
        (
            f"vendite: {meta['n_sales']}  invenduti: {meta['n_unsold']}  "
            f"squadre: {meta['teams']}  budget: {meta['budget']}  "
            f"listone: {meta['n_players']} giocatori"
        ),
        (
            f"gate: min_sample={meta['gate_config']['min_sample']}  "
            f"mape_improvement_min={meta['gate_config']['mape_improvement_min']}  "
            f"spearman_min={meta['gate_config']['spearman_min']}"
        ),
        "",
        "GATE QUALITA' CALIBRAZIONE vs MIGLIORE BASELINE",
        f"  passed: {verdict['passed']}",
        (
            f"  baseline migliore: {gate['best_baseline']} "
            f"(MAPE globale {gate['best_baseline_mape']})"
        ),
        (
            f"  use_calibration_in_price: {gate['use_calibration_in_price']} "
            "(il gate NON attiva nulla: serve decisione esplicita)"
        ),
    ]
    if verdict["reasons"]:
        lines.append("  motivi del non-pass:")
        for reason in verdict["reasons"]:
            lines.append(f"    - {reason}")
    lines += ["", "CONFRONTO GLOBALE (prezzo reale vs predetto)"]
    lines.append(
        f"  {'modello':<12}{'n':>6}{'MAE':>12}{'RMSE':>12}{'MAPE':>12}"
        f"{'Spearman':>12}{'coverage':>12}"
    )
    for model in MODELS:
        m = models[model]["global"][""]
        lines.append(
            f"  {model:<12}{m['n']:>6}"
            f"{_csv_cell(m['mae']):>12}{_csv_cell(m['rmse']):>12}"
            f"{_csv_cell(m['mape']):>12}{_csv_cell(m['spearman']):>12}"
            f"{_csv_cell(m['coverage']):>12}"
        )
    lines.append(
        "\nmetriche non valide (NaN/None) = non computabili sul campione "
        "(mai un numero finto)"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Backtest prezzo d'asta in replay prequenziale (WP9)"
    )
    ap.add_argument(
        "--sales",
        required=True,
        help="CSV storico vendite oppure backup JSON dell'event store",
    )
    ap.add_argument("--listone", default=DEFAULT_LISTONE, help="CSV canonico listone")
    ap.add_argument("--teams", type=int, help="squadre della lega (default config)")
    ap.add_argument("--budget", type=int, help="budget per squadra (default config)")
    ap.add_argument("--io", help="nome della propria squadra (default config)")
    ap.add_argument(
        "--season", default=None, help="filtra le vendite a una sola stagione"
    )
    ap.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help="directory o base path dei report (default: data/)",
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=gates.MIN_SAMPLE_N,
        help="gate: campione minimo (default 30)",
    )
    ap.add_argument(
        "--mape-improvement",
        type=float,
        default=gates.MAPE_IMPROVEMENT_MIN,
        help="gate: miglioramento MAPE minimo vs baseline (default 0.05)",
    )
    ap.add_argument(
        "--spearman-min",
        type=float,
        default=gates.SPEARMAN_MIN,
        help="gate: Spearman OOS minimo (default 0.30)",
    )
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    gate_cfg = gates.GateConfig(
        min_sample=args.min_n,
        mape_improvement_min=args.mape_improvement,
        spearman_min=args.spearman_min,
    )
    if gate_cfg.validate():
        print(
            "ERRORE: soglie di gate invalide: " + "; ".join(gate_cfg.validate()),
            file=sys.stderr,
        )
        return 2

    # --- listone ---
    try:
        players = load_players(args.listone)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2
    if not players:
        print(f"ERRORE: listone vuoto: {args.listone}", file=sys.stderr)
        return 2

    # --- config lega (profilo "engine": il backtest RIGIOCA uno storico,
    # non redige una rosa ora — la fattibilità di pool/budget dell'entry
    # point live non si applica a un replay su listone parziale; le
    # invarianti strutturali valgono identiche e il costruttore del motore
    # ri-valida comunque) ---
    overrides: dict[str, Any] = {}
    if args.teams is not None:
        overrides["teams"] = args.teams
    if args.budget is not None:
        overrides["budget"] = args.budget
    if args.io:
        overrides["io"] = args.io
    cfg = normalize(overrides)
    errors = list(validate(cfg, profile="engine"))
    if errors:
        print("ERRORE: config di lega invalida: " + "; ".join(errors), file=sys.stderr)
        return 2

    # --- vendite ---
    by_pid = {p["pid"]: p for p in players}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in players:
        by_name.setdefault(norm(p["nome"]), []).append(p)
    try:
        events = parse_sales(args.sales, by_pid, by_name, cfg["team_names"])
        sort_sales(events)
        check_duplicates(events)
    except (FileNotFoundError, SalesError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2
    if args.season is not None:
        events = [ev for ev in events if ev.season == args.season]
    n_sold = sum(1 for ev in events if ev.kind == "sold")
    if n_sold < 1:
        print(
            "ERRORE: nessuna vendita valida nel file vendite (dopo filtri): "
            f"{args.sales}",
            file=sys.stderr,
        )
        return 2

    # --- motore e replay ---
    try:
        auction = Auction(
            players, teams=cfg["teams"], budget=cfg["budget"], io=cfg["io"]
        )
    except ConfigError as e:
        print(
            f"ERRORE: motore non costruibole con la config fornita: {e}",
            file=sys.stderr,
        )
        return 2
    cal_cfg, cal_errors = calibration.parse_calibration_params(cfg)
    if cal_errors:
        print(
            "ERRORE: parametri calibrazione invalidi: " + "; ".join(cal_errors),
            file=sys.stderr,
        )
        return 2
    try:
        predictions = run_replay(auction, events, cal_cfg, cfg)
    except SalesError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2

    # --- metriche e gate ---
    models = {model: slice_metrics(predictions, model) for model in MODELS}
    gate = evaluate_auction_gate(predictions, models, gate_cfg)
    meta = {
        "n_players": len(players),
        "n_sales": len(predictions),
        "n_unsold": sum(1 for ev in events if ev.kind == "unsold"),
        "teams": cfg["teams"],
        "budget": cfg["budget"],
        "sales_file": args.sales,
        "listone": args.listone,
        "season_filter": args.season,
        "gate_config": gate_cfg.to_dict(),
        "baselines": list(BASELINES),
        "gate_mai_passa_su": [
            "NaN/metriche non valide",
            "n insufficiente",
            "leakage",
        ],
    }

    # --- report atomici ---
    json_path, csv_path, txt_path = report_paths(args.out)
    write_atomic(json_path, build_json_report(meta, models, gate, predictions))
    write_atomic(csv_path, build_csv_report(models))
    write_atomic(txt_path, build_txt_report(meta, models, gate))
    print(f"report scritti: {json_path} {csv_path} {txt_path}")
    verdict = gate["verdict"]
    print(
        f"gate: {'PASS' if verdict['passed'] else 'NO-PASS'} "
        f"({'; '.join(verdict['reasons']) or 'tutti i controlli ok'})"
    )
    print(
        f"use_calibration_in_price = {gate['use_calibration_in_price']} "
        "(decisione esplicita richiesta, mai automatica)"
    )
    return 0


def txt_path_from(json_path: str) -> str:
    return json_path[:-5] + ".txt"


if __name__ == "__main__":
    sys.exit(main())
