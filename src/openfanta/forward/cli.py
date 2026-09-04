#!/usr/bin/env -S uv run --quiet python
"""CLI del simulatore della fase finale dell'asta Attaccanti (Monte Carlo lean).

Uso:
  openfanta-forward --snapshot data/forward_snapshot_a.json \
      [--runs 10000] [--seed 42] [--player-order shuffle|by_value] \
      [--floor 2] [--json-out data/forward_report_attaccanti.json] \
      [--cache-dir data/forward_cache] [--no-cache] [--force] \
      [--deterministic-report] [--demo] [--values data/forward_values.json]
  openfanta-forward snapshot --csv data/listone.csv \
      --squadre 8 --budget 500 [--sales "DIMARCO 300 IO, PAZ N. 120 T1"] \
      --out data/forward_snapshot_a.json

Exit code: 0 = ok (con warning su stderr); 2 = snapshot/config invalido
(messaggio su stderr, nessun output). Read-only verso il motore live.

Cache content-addressed: key = sha256(canonical_json({schema_version, snapshot,
cfg})); file in ``data/forward_cache/<key>.json`` scritto atomicamente
(temp+rename) e riusato a parita' di chiave (``--no-cache``/``--force`` per
bypassare). ``--deterministic-report`` => nessun timestamp: report
byte-identico a parita' di input.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from openfanta.forward.agg import report, snapshot_key
from openfanta.forward.bidding import BidConfig
from openfanta.forward.sim import simulate
from openfanta.forward.state import snapshot_from_auction, validate_snapshot

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_DIR = str(REPO_ROOT)
DEFAULT_CSV = os.path.join(BASE_DIR, "data", "listone.csv")
DEFAULT_SNAPSHOT_OUT = os.path.join(BASE_DIR, "data", "forward_snapshot_a.json")
DEFAULT_JSON_OUT = os.path.join(BASE_DIR, "data", "forward_report_attaccanti.json")
DEFAULT_CACHE_DIR = os.path.join(BASE_DIR, "data", "forward_cache")


def _cfg_from_args(args: argparse.Namespace) -> tuple[BidConfig, list[str]]:
    cfg = BidConfig(
        n_runs=args.runs,
        seed=args.seed,
        player_order=args.player_order,
        floor_price=args.floor,
    )
    errs = cfg.validate()
    return cfg, errs


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ValueError(f"{path}: file non leggibile ({e})") from e
    except ValueError as e:  # JSONDecodeError
        raise ValueError(f"{path}: JSON malformato ({e})") from e
    if not isinstance(data, dict):
        raise TypeError(f"{path}: atteso un oggetto JSON")
    return data


def _write_atomic(path: str, text: str) -> None:
    """Scrittura atomica (temp + rename) — mai file cache parziali su crash."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"impossibile creare {d}: {e}") from e
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _finalize(payload: dict, *, deterministic_report: bool) -> dict:
    out = dict(payload)
    if not deterministic_report:
        out["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------- cache
class _Cache:
    """Cache in-process + su disco content-addressed."""

    def __init__(self, cache_dir: str, enabled: bool):
        self.dir = cache_dir
        self.enabled = enabled
        self.memory: dict[str, dict] = {}

    def path_for(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        if key in self.memory:
            return self.memory[key]
        path = self.path_for(key)
        if os.path.exists(path):
            try:
                payload = _load_json(path)
            except (OSError, TypeError, ValueError):
                return None
            self.memory[key] = payload
            return payload
        return None

    def put(self, key: str, payload: dict) -> None:
        self.memory[key] = payload
        if self.enabled:
            _write_atomic(self.path_for(key), _dump(payload))


# ------------------------------------------------------------- simulate/report
def _run_simulation(snapshot: dict, cfg: BidConfig) -> dict:
    result = simulate(snapshot, cfg)
    if isinstance(result, tuple):
        result = result[0]
    return report(result, snapshot, cfg, deterministic_report=True)


# ------------------------------------------------------------- demo
def _demo_snapshot() -> dict:
    from openfanta.core.auction import Auction, load_players  # sola lettura

    players = load_players(DEFAULT_CSV)
    auction = Auction(players, teams=8, budget=500)
    # vendite deterministiche di attaccanti "fase finale"
    sales = [
        ("MALEN", 300, "IO"),
        ("DIAO", 180, "T1"),
        ("LUCCA", 120, "T2"),
        ("MARTINEZ L.", 250, "T3"),
    ]
    for nome, price, team in sales:
        p = auction.find(nome)
        auction.mark_sold(p, price, team)
    return snapshot_from_auction(auction)


def _cmd_demo(args: argparse.Namespace) -> int:
    snapshot = _demo_snapshot()
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    cfg = BidConfig(
        n_runs=min(args.runs, 2000), seed=args.seed, player_order=args.player_order
    )
    payload = _run_simulation(snapshot, cfg)
    print(_dump(_finalize(payload, deterministic_report=args.deterministic_report)))
    return 0


# ------------------------------------------------------------- snapshot builder
def _parse_sales(sales: str | None) -> list[tuple[str, int, str]]:
    if not sales:
        return []
    out = []
    for part in sales.split(","):
        part = part.strip()
        if not part:
            continue
        toks = part.split()
        if len(toks) < 3 or not toks[-2].isdigit():
            raise ValueError(
                f"vendita non valida: {part!r} (formato: NOME PREZZO SQUADRA)"
            )
        try:
            price = int(toks[-2])
        except ValueError as e:
            raise ValueError(f"vendita non valida: {part!r} (prezzo non intero)") from e
        out.append((" ".join(toks[:-2]), price, toks[-1]))
    return out


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from openfanta.core.auction import Auction, load_players  # sola lettura

    players = load_players(args.csv)
    auction = Auction(players, teams=args.squadre, budget=args.budget)
    try:
        for nome, price, team in _parse_sales(args.sales):
            p = auction.find(nome)
            if p is None:
                raise ValueError(f"giocatore non trovato: {nome}")
            auction.mark_sold(p, price, team)
    except ValueError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 2
    snapshot = snapshot_from_auction(auction)
    _write_atomic(args.out, _dump(snapshot))
    print(f"Snapshot scritto: {args.out}", file=sys.stderr)
    return 0


# ------------------------------------------------------------- main run
def _cmd_run(args: argparse.Namespace) -> int:
    cfg, errs = _cfg_from_args(args)
    if errs:
        print("Configurazione non valida: " + "; ".join(errs), file=sys.stderr)
        return 2
    try:
        snapshot = _load_json(args.snapshot)
    except (OSError, TypeError, ValueError) as e:
        print(f"Snapshot non leggibile: {e}", file=sys.stderr)
        return 2
    errs = validate_snapshot(snapshot, cfg)
    if errs:
        print("Snapshot invalido: " + "; ".join(errs), file=sys.stderr)
        return 2

    if args.values:
        try:
            values = _load_json(args.values)
        except (OSError, TypeError, ValueError) as e:
            print(f"Valori non leggibili: {e}", file=sys.stderr)
            return 2
        by_pid = {p["pid"]: p for p in snapshot["players"]}
        for pid, v in values.items():
            if pid in by_pid and isinstance(v, dict):
                by_pid[pid]["value"] = v.get("value")
                by_pid[pid]["rank"] = v.get("rank")

    cache = _Cache(args.cache_dir, enabled=not args.no_cache and not args.force)
    key = snapshot_key(snapshot, cfg)
    payload = cache.get(key)
    if payload is None:
        payload = _run_simulation(snapshot, cfg)
        cache.put(key, payload)
        source = "calcolo"
    else:
        source = "cache"

    out = _finalize(payload, deterministic_report=args.deterministic_report)
    text = _dump(out)
    if args.json_out:
        _write_atomic(args.json_out, text)
    else:
        print(text, end="")

    warnings = list(payload.get("warnings", []))
    if warnings:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
    print(
        f"# {source}, key={key[:12]}..., runs={cfg.n_runs}, seed={cfg.seed}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="forward_simulator",
        description="Simulatore Monte Carlo della fase finale dell'asta Attaccanti",
    )
    ap.add_argument("--snapshot", default=None, help="snapshot JSON v1 in ingresso")
    ap.add_argument(
        "--runs", type=int, default=10_000, help="numero di run Monte Carlo"
    )
    ap.add_argument("--seed", type=int, default=42, help="seed deterministico")
    ap.add_argument(
        "--player-order",
        default="shuffle",
        choices=["shuffle", "by_value"],
        help="ordine di nomina per run",
    )
    ap.add_argument(
        "--floor", type=int, default=2, help="floor vendita/riserva ruolo A"
    )
    ap.add_argument(
        "--json-out", default=None, help="file JSON di output (default: stdout)"
    )
    ap.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help="directory cache content-addressed",
    )
    ap.add_argument(
        "--no-cache", action="store_true", help="disabilita la cache su disco"
    )
    ap.add_argument(
        "--force", action="store_true", help="ricalcola anche se chiave cache presente"
    )
    ap.add_argument(
        "--deterministic-report",
        action="store_true",
        help="nessun timestamp: report byte-identico a parita' di input",
    )
    ap.add_argument(
        "--demo",
        action="store_true",
        help="snapshot sintetico 'fase finale' dal listone reale + report breve",
    )
    ap.add_argument(
        "--values",
        default=None,
        help="JSON {pid: {value, rank}} — input modello/ranking, mai nel prezzo",
    )

    sub = ap.add_subparsers(dest="subcommand")
    sub_snap = sub.add_parser("snapshot", help="esporta la snapshot da un'asta live")
    sub_snap.add_argument("--csv", default=DEFAULT_CSV, help="CSV canonico del listone")
    sub_snap.add_argument("--squadre", type=int, default=8)
    sub_snap.add_argument("--budget", type=int, default=500)
    sub_snap.add_argument(
        "--sales",
        default=None,
        help='vendite "NOME PREZZO SQUADRA, NOME2 PREZZO2 SQUADRA2, ..."',
    )
    sub_snap.add_argument("--out", default=DEFAULT_SNAPSHOT_OUT)

    args = ap.parse_args(argv)

    if args.subcommand == "snapshot":
        return _cmd_snapshot(args)
    if args.demo:
        return _cmd_demo(args)
    if not args.snapshot:
        print("Serve --snapshot <file> (oppure --demo)", file=sys.stderr)
        return 2
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
