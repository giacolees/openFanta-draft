#!/usr/bin/env -S uv run --quiet python
"""Backtest del RENDIMENTO stagionale, separato dal prezzo d'asta (WP9).

Domanda: "quanto rende cio' che e' stato pagato?" — il backtest valuta il
**valore fantacalcistico realizzato** (FM della stagione) per credito speso
(yield = somma FM realizzate / somma prezzi), per squadra, ruolo e banda,
piu' la capacita' dei columni del listone (PFC/PMA/expfm) di prevedere il
rendimento realizzato (Spearman/MAE). Baseline dichiarate: PFC e PMA
(il listone stesso); riferimento "casuale" = yield medio globale.

Input:

- ``--sales PATH`` — CSV storico vendite o backup JSON dell'event store
  (stessi formati/validazioni di ``backtest_auction.py``);
- ``--outcomes PATH`` — CSV dei rendimenti realizzati con schema
  ``pid,nome,realized_fm,minutes,season``: ``pid`` preferito, ``nome``
  fallback legacy; ``realized_fm`` accetta anche l'alias ``points``;
  ``minutes`` opzionale; ``season`` opzionale ma necessaria per valutare
  l'out-of-sample del gate;
- ``--listone PATH`` — libro canonico (PFC/PMA/expfm/ruolo/banda).

Modalita' target:

- ``realized_fm`` — rendimento reale (dal file outcomes);
- ``proxy_expfm`` — SE il file outcomes non contiene alcuna colonna di
  rendimento, il backtest degrada a usare l'``expfm`` del listone come
  target: e' ETICHETTATO come proxy nel report e il gate resta FALSE
  (l'expected del listone non e' un rendimento realizzato).

Gate di qualita' (recomendazione, mai attivazione): mai pass con

- campione insufficiente (n < min_sample, default 30);
- Spearman out-of-sample < soglia (default 0.30);
- target proxy expfm;
- STESSA stagione tra vendite e outcomes (in-sample/leakage);
- stagioni non dichiarate (out-of-sample non verificabile).

Output atomici: ``<out>/backtest_yield_report.json/.csv/.txt``. Exit
0 = report scritti; exit 2 = input mancante/invalido o nessun dato unito.
Nessuna rete, nessun timestamp nel contenuto: output deterministico. Il
``gate_recommendation`` NON abilita mai nulla: ``use_calibration_in_price``
resta False finche' una decisione esplicita futura non lo consente.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any

from openfanta.backtest.price import (
    AuctionEvent,
    SalesError,
    check_duplicates,
    parse_sales,
    sort_sales,
    write_atomic,
)
from openfanta.core import calibration, gates
from openfanta.core.auction import load_players
from openfanta.core.config import norm

DEFAULT_LISTONE = "data/listone.csv"
DEFAULT_OUT_DIR = "data"
DEFAULT_REPORT_BASE = "backtest_yield_report"

# Colonne di rendimento realizzato accettate (la prima che esiste vince).
REALIZED_COLUMNS = ("realized_fm", "points")

RANK_MODELS = ("pfc", "pma", "expfm")


class OutcomesError(Exception):
    """Errore di input nel file outcomes (riga contestualizzata, bloccante)."""


# ---------------------------------------------------------------------------
# parsing outcomes
# ---------------------------------------------------------------------------
def _norm_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _resolve_outcome_player(
    row: dict[str, str],
    i: int,
    by_pid: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Giocatore da una riga outcome: pid preferito, nome legacy fallback."""
    where = f"riga {i + 2} del CSV outcomes"
    pid = _norm_str(row.get("pid"))
    if pid:
        p = by_pid.get(pid)
        if p is None:
            raise OutcomesError(f"{where}: giocatore assente dal listone (pid={pid!r})")
        return p
    nome = _norm_str(row.get("nome"))
    nome_key = norm(nome)
    if not nome_key:
        raise OutcomesError(f"{where}: riga senza pid ne' nome")
    candidates = by_name.get(nome_key, [])
    if not candidates:
        raise OutcomesError(f"{where}: giocatore assente dal listone (nome={nome!r})")
    if len(candidates) > 1:
        raise OutcomesError(
            f"{where}: nome ambiguo nel listone (nome={nome!r}): "
            f"{len(candidates)} giocatori; serve il pid"
        )
    return candidates[0]


def _parse_realized(row: dict[str, str], i: int) -> float:
    """Rendimento realizzato della riga: prima colonna riconosciuta tra
    ``realized_fm`` e ``points``; errore chiaro se assente o non numerico."""
    where = f"riga {i + 2} del CSV outcomes"
    for col in REALIZED_COLUMNS:
        text = _norm_str(row.get(col))
        if not text:
            continue
        try:
            value = float(text)
        except ValueError as e:
            raise OutcomesError(
                f"{where}: {col} non valido (atteso numero): {text!r}"
            ) from e
        if value < 0:
            raise OutcomesError(f"{where}: {col} non puo' essere negativo ({value})")
        return value
    raise OutcomesError(
        f"{where}: rendimento realizzato mancante (colonne accettate: "
        f"{', '.join(REALIZED_COLUMNS)})"
    )


def parse_outcomes(
    path: str,
    by_pid: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], bool]:
    """CSV outcomes -> righe normalizzate. Ritorna (righe, proxy_mode).

    ``proxy_mode`` e' True solo se il CSV non ha NESSUNA colonna di
    rendimento: il target diventa l'expfm del listone, etichettato proxy."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"file outcomes non trovato: {path}") from e
    if not rows:
        return [], False
    proxy_mode = not any(col in fieldnames for col in REALIZED_COLUMNS)
    outcomes: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        p = _resolve_outcome_player(row, i, by_pid, by_name)
        realized = None if proxy_mode else _parse_realized(row, i)
        outcomes.append(
            {
                "pid": p["pid"],
                "nome": p["nome"],
                "ruolo": p["ruolo"],
                "pfc": p["pfc"],
                "pma": p["pma"],
                "expfm": p["expfm"],
                "base": max(1, round(p["pfc"])),
                "realized_fm": realized,
                "minutes": _norm_str(row.get("minutes")) or None,
                "season": _norm_str(row.get("season")),
            }
        )
    return outcomes, proxy_mode


# ---------------------------------------------------------------------------
# join vendite x outcomes
# ---------------------------------------------------------------------------
def join_sales_outcomes(
    events: list[AuctionEvent], outcomes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Unisce vendite e rendimenti: una riga per vendita con outcome risolvibile.

    Regola di join (deterministica, mai ambigua):

    - vendita con stagione: preferito l'outcome della STESSA stagione; se
      assente, accettato un outcome UNICO di stagione diversa (validazione
      retrospettiva, il gate valuta comunque la stagione nel giudizio);
    - vendita senza stagione: unita solo se il giocatore ha UN outcome unico.

    Ritorna (righe unite, motivi informativi per le vendite scartate)."""
    by_pid: dict[str, list[dict[str, Any]]] = {}
    for o in outcomes:
        by_pid.setdefault(o["pid"], []).append(o)
    joined: list[dict[str, Any]] = []
    skipped: list[str] = []
    for ev in events:
        if ev.kind != "sold" or ev.price is None:
            continue
        candidates = by_pid.get(ev.pid, [])
        match = None
        if ev.season:
            matches = [o for o in candidates if o["season"] == ev.season]
            if not matches:
                # fallback: un outcome unico di stagione diversa e' accettato
                if len(candidates) == 1:
                    match = candidates[0]
                elif len(candidates) > 1:
                    skipped.append(
                        f"{ev.nome}: nessun outcome per la stagione "
                        f"{ev.season!r} e {len(candidates)} outcomes di "
                        "altre stagioni: join non risolvibile"
                    )
                    continue
                else:
                    skipped.append(
                        f"{ev.nome}: nessun outcome nel file per la stagione "
                        f"{ev.season!r}"
                    )
                    continue
            elif len(matches) > 1:
                raise OutcomesError(
                    f"outcomes duplicati per {ev.nome} nella stagione "
                    f"{ev.season!r}: il join sarebbe ambiguo"
                )
            else:
                match = matches[0]
        else:
            if not candidates:
                skipped.append(f"{ev.nome}: nessun outcome nel file")
                continue
            if len(candidates) > 1:
                skipped.append(
                    f"{ev.nome}: vendita senza stagione e {len(candidates)} "
                    "outcomes: join non risolvibile (dichiarare la stagione)"
                )
                continue
            match = candidates[0]
        target = (
            match["realized_fm"] if match["realized_fm"] is not None else match["expfm"]
        )
        joined.append(
            {
                "pid": ev.pid,
                "nome": ev.nome,
                "ruolo": ev.ruolo,
                "team": ev.team,
                "price": ev.price,
                "season_sale": ev.season,
                "season_outcome": match["season"],
                "pfc": match["pfc"],
                "pma": match["pma"],
                "expfm": match["expfm"],
                "realized_fm": target,
            }
        )
    return joined, skipped


def _yield_rows(
    joined: list[dict[str, Any]], key: str | None
) -> dict[str, dict[str, Any]]:
    """Yield per slice: n, somma prezzi, somma FM realizzate, FM/credito."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in joined:
        label = "globale" if key is None else str(row[key])
        groups.setdefault(label, []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for label in sorted(groups):
        rows = groups[label]
        sum_price = sum(r["price"] for r in rows)
        sum_realized = sum(r["realized_fm"] for r in rows)
        out[label] = {
            "n": len(rows),
            "sum_price": round(sum_price, 4),
            "sum_realized_fm": round(sum_realized, 4),
            "yield": (round(sum_realized / sum_price, 4) if sum_price > 0 else None),
        }
    return out


def _ranking_metrics(joined: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Capacita' del columno del listone di prevedere il rendimento realizzato.

    ``mae`` e' computabile solo tra grandezze comparable (expfm vs
    realized_fm, entrambe fantamedie): per PFC/PMA resta null."""
    actuals = [r["realized_fm"] for r in joined]
    preds = [r[model] for r in joined]
    spearman = gates.spearman(actuals, preds)
    mae = gates.mae(actuals, preds) if model == "expfm" else None
    return {
        "n": len(joined),
        "spearman": round(spearman, 4) if spearman is not None else None,
        "mae": round(mae, 4) if mae is not None else None,
    }


def yield_gate_reasons(joined: list[dict[str, Any]], proxy_mode: bool) -> list[str]:
    """Motivi che rendono il pass impossibile (proxy, in-sample, stagioni)."""
    reasons: list[str] = []
    if proxy_mode:
        reasons.append(
            "rendimento etichettato proxy: target = expfm del listone, "
            "non un rendimento realizzato"
        )
    sale_seasons = {r["season_sale"] for r in joined if r["season_sale"]}
    outcome_seasons = {r["season_outcome"] for r in joined if r["season_outcome"]}
    same_season = {
        r["season_sale"]
        for r in joined
        if r["season_sale"] and r["season_sale"] == r["season_outcome"]
    }
    if same_season:
        reasons.append(
            "stessa stagione tra vendite e outcomes "
            f"({', '.join(sorted(same_season))}): valutazione in-sample, "
            "nessuna evidenza out-of-sample"
        )
    if not sale_seasons and not same_season:
        reasons.append(
            "stagione delle vendite non dichiarata: out-of-sample non verificabile"
        )
    if not outcome_seasons and not same_season:
        reasons.append(
            "stagione degli outcomes non dichiarata: out-of-sample non verificabile"
        )
    return reasons


def evaluate_yield_gate(
    joined: list[dict[str, Any]],
    proxy_mode: bool,
    gate_cfg: gates.GateConfig,
) -> dict[str, Any]:
    """Gate del rendimento: Spearman OOS (PFC vs realizzato), n, no proxy,
    no stessa stagione, stagioni dichiarate. Solo raccomandazione, mai
    attivazione."""
    extra = yield_gate_reasons(joined, proxy_mode)
    pfc_rank = _ranking_metrics(joined, "pfc")
    verdict = gates.evaluate_gate(
        n=len(joined),
        mape_model=None,
        mape_baseline=None,
        spearman=pfc_rank["spearman"],
        leakage=False,
        extra_reasons=extra,
        config=gate_cfg,
        require_mape=False,
    )
    return {
        "verdict": verdict.to_dict(),
        "target": "proxy_expfm" if proxy_mode else "realized_fm",
        "use_calibration_in_price": False,
        "note": (
            "il gate NON attiva nulla: use_calibration_in_price resta False "
            "finche' una decisione esplicita futura non lo consente"
        ),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def build_json_report(
    meta: dict[str, Any],
    ranking: dict[str, Any],
    yield_report: dict[str, Any],
    gate: dict[str, Any],
    joined: list[dict[str, Any]],
) -> str:
    doc = {
        "meta": meta,
        "ranking_vs_outcome": ranking,
        "yield": yield_report,
        "gate_recommendation": gate,
        "joined": joined,
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def build_csv_report(ranking: dict[str, Any], yield_report: dict[str, Any]) -> str:
    lines = [
        "kind,model_or_slice_type,slice,n,spearman,mae,sum_price,sum_realized_fm,yield"
    ]
    for model in RANK_MODELS:
        for slice_type in ("global", "role"):
            slices = ranking[model][slice_type]
            for value in sorted(slices):
                m = slices[value]
                lines.append(
                    ",".join(
                        _csv_cell(v)
                        for v in (
                            "ranking",
                            f"{model}:{slice_type}",
                            value,
                            m["n"],
                            m["spearman"],
                            m["mae"],
                            "",
                            "",
                            "",
                        )
                    )
                )
    for label in ("global", "team", "role", "band"):
        slices = yield_report[label]
        for value in sorted(slices):
            m = slices[value]
            lines.append(
                ",".join(
                    _csv_cell(v)
                    for v in (
                        "yield",
                        label,
                        value,
                        m["n"],
                        "",
                        "",
                        m["sum_price"],
                        m["sum_realized_fm"],
                        m["yield"],
                    )
                )
            )
    return "\n".join(lines) + "\n"


def build_txt_report(
    meta: dict[str, Any],
    ranking: dict[str, Any],
    yield_report: dict[str, Any],
    gate: dict[str, Any],
) -> str:
    verdict = gate["verdict"]
    lines: list[str] = [
        "Backtest rendimento stagionale (WP9) — separato dal prezzo",
        "=" * 58,
        (
            f"vendite unite con outcome: {meta['n_joined']}/{meta['n_sales']}  "
            f"target: {gate['target']}  baselines: {', '.join(meta['baselines'])}"
        ),
        "",
        "GATE QUALITA' RENDIMENTO (Spearman OOS PFC vs realizzato)",
        f"  passed: {verdict['passed']}",
        (
            f"  use_calibration_in_price: {gate['use_calibration_in_price']} "
            "(il gate NON attiva nulla: serve decisione esplicita)"
        ),
    ]
    if verdict["reasons"]:
        lines.append("  motivi del non-pass:")
        for reason in verdict["reasons"]:
            lines.append(f"    - {reason}")
    lines += ["", "RANKING vs RENDIMENTO REALIZZATO (Spearman)"]
    lines.append(f"  {'columno':<10}{'n':>6}{'Spearman':>12}{'MAE':>12}")
    for model in RANK_MODELS:
        m = ranking[model]["global"][""]
        lines.append(
            f"  {model:<10}{m['n']:>6}"
            f"{_csv_cell(m['spearman']):>12}{_csv_cell(m['mae']):>12}"
        )
    lines += ["", "YIELD (FM realizzate per credito speso)"]
    for label in ("global", "role", "band", "team"):
        slices = yield_report[label]
        if not slices:
            continue
        lines.append(f"  [{label}]")
        lines.append(
            f"    {'slice':<16}{'n':>6}{'Σ price':>12}{'Σ FM':>12}{'yield':>10}"
        )
        for value in sorted(slices):
            m = slices[value]
            lines.append(
                f"    {value:<16}{m['n']:>6}"
                f"{_csv_cell(m['sum_price']):>12}{_csv_cell(m['sum_realized_fm']):>12}"
                f"{_csv_cell(m['yield']):>10}"
            )
    lines.append(
        "\nyield medio globale = riferimento 'casuale': comprare a caso il "
        "mercato rende quanto la media del campione"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Backtest rendimento stagionale separato dal prezzo (WP9)"
    )
    ap.add_argument(
        "--sales",
        required=True,
        help="CSV storico vendite oppure backup JSON event store",
    )
    ap.add_argument(
        "--outcomes",
        required=True,
        help="CSV rendimenti realizzati (pid,nome,realized_fm,minutes,season)",
    )
    ap.add_argument("--listone", default=DEFAULT_LISTONE, help="CSV canonico listone")
    ap.add_argument("--season", default=None, help="filtra le vendite a una stagione")
    ap.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help="directory o base path dei report (default: data/)",
    )
    ap.add_argument(
        "--teams",
        type=int,
        default=None,
        help="squadre tracciate per lo yield per squadra (default: nessuna, tutti ALTRO)",
    )
    ap.add_argument(
        "--io", default=None, help="nome della propria squadra (default 'IO')"
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=gates.MIN_SAMPLE_N,
        help="gate: campione minimo (default 30)",
    )
    ap.add_argument(
        "--spearman-min",
        type=float,
        default=gates.SPEARMAN_MIN,
        help="gate: Spearman OOS minimo (default 0.30)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gate_cfg = gates.GateConfig(
        min_sample=args.min_n,
        spearman_min=args.spearman_min,
    )
    if gate_cfg.validate():
        print(
            "ERRORE: soglie di gate invalide: " + "; ".join(gate_cfg.validate()),
            file=sys.stderr,
        )
        return 2

    try:
        players = load_players(args.listone)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2
    if not players:
        print(f"ERRORE: listone vuoto: {args.listone}", file=sys.stderr)
        return 2
    by_pid = {p["pid"]: p for p in players}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in players:
        by_name.setdefault(norm(p["nome"]), []).append(p)

    try:
        events = parse_sales(args.sales, by_pid, by_name, team_names(args))
        check_duplicates(events)
        outcomes, proxy_mode = parse_outcomes(args.outcomes, by_pid, by_name)
    except (FileNotFoundError, SalesError, OutcomesError) as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2
    if args.season is not None:
        events = [ev for ev in events if ev.season == args.season]
    events = sort_sales(events)
    n_sales = sum(1 for ev in events if ev.kind == "sold")
    if n_sales < 1:
        print(
            f"ERRORE: nessuna vendita valida nel file: {args.sales}",
            file=sys.stderr,
        )
        return 2
    if not outcomes:
        print(f"ERRORE: nessun outcome nel file: {args.outcomes}", file=sys.stderr)
        return 2

    try:
        joined, skipped = join_sales_outcomes(events, outcomes)
    except OutcomesError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        return 2
    if not joined:
        print(
            "ERRORE: nessuna vendita unita con un outcome (nessun dato): "
            "controlla pid/nome/stagione tra vendite e outcomes",
            file=sys.stderr,
        )
        return 2

    cal_cfg = calibration.CalibrationConfig()
    ranking = {
        model: {
            "global": {"": _ranking_metrics(joined, model)},
            "role": {},
        }
        for model in RANK_MODELS
    }
    for model in RANK_MODELS:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in joined:
            groups.setdefault(row["ruolo"], []).append(row)
        for role in sorted(groups):
            ranking[model]["role"][role] = _ranking_metrics(groups[role], model)

    yield_report = {
        "global": {"globale": _yield_rows(joined, None)["globale"]},
        "role": _yield_rows(joined, "ruolo"),
        "band": _band_rows(joined, cal_cfg),
        "team": _yield_rows(joined, "team"),
    }
    gate = evaluate_yield_gate(joined, proxy_mode, gate_cfg)
    meta = {
        "sales_file": args.sales,
        "outcomes_file": args.outcomes,
        "listone": args.listone,
        "n_players": len(players),
        "n_sales": n_sales,
        "n_joined": len(joined),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "target": "proxy_expfm" if proxy_mode else "realized_fm",
        "baselines": ["PFC", "PMA"],
        "gate_config": gate_cfg.to_dict(),
        "gate_mai_passa_su": [
            "n insufficiente",
            "Spearman OOS sotto soglia",
            "proxy expfm",
            "stessa stagione (in-sample/leakage)",
            "stagioni non dichiarate",
        ],
    }
    if proxy_mode:
        meta["proxy_note"] = (
            "target proxy: l'expfm del listone sostituisce il rendimento "
            "realizzato; il gate resta false"
        )

    json_path, csv_path, txt_path = _paths(args.out)
    write_atomic(
        json_path, build_json_report(meta, ranking, yield_report, gate, joined)
    )
    write_atomic(csv_path, build_csv_report(ranking, yield_report))
    write_atomic(txt_path, build_txt_report(meta, ranking, yield_report, gate))
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


def team_names(args: argparse.Namespace) -> list[str]:
    """Nomi canonici delle squadre tracciate (o lista vuota: tutti ALTRO).

    Senza ``--teams`` non si simula nessuna rosa: l'acquirente resta
    "ALTRO" esplicito e le invarianti di budget restano rispettate."""
    if args.teams is None:
        return []
    from openfanta.core.config import normalize

    cfg = normalize({"teams": args.teams, "io": args.io or "IO"})
    return list(cfg["team_names"])


def _band_rows(joined: list[dict[str, Any]], cal_cfg: calibration.CalibrationConfig):
    labeled = [
        dict(r, band=calibration.band_for_base(r["pfc"], cal_cfg)) for r in joined
    ]
    return _yield_rows(labeled, "band")


def _paths(out: str) -> tuple[str, str, str]:
    if out.lower().endswith(".json"):
        base = out[:-5]
    else:
        base = os.path.join(out, DEFAULT_REPORT_BASE)
    return base + ".json", base + ".csv", base + ".txt"


if __name__ == "__main__":
    sys.exit(main())
