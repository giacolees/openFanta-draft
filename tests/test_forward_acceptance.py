"""Acceptance end-to-end (WP forward) — vedi §12.6.

Snapshot dal listone reale -> CLI 10k run -> exit 0, schema v1, wall time < 45s;
determinismo byte-identico (cmp); snapshot invalido -> exit 2 senza output;
cache content-addressed (seconda run = cache hit, snapshot modificato = nuovo
calcolo). L'unico file esterno letto e' ``data/listone.csv``; gli output vanno
in tmp (mai persistenza in ``data/`` durante i test).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from openfanta.forward.state import (  # pyright: ignore[reportMissingImports]
    snapshot_from_auction,
)

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
CLI = SRC / "openfanta" / "forward" / "cli.py"
LISTONE = REPO / "data" / "listone.csv"


def _cli_env() -> dict[str, str]:
    """Env per il CLI in subprocess: ``src/`` in PYTHONPATH cosi' gli import
    assoluti ``openfanta.*`` si risolvono anche senza package installato."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


SALES = (
    "MALEN 300 IO, MARTINEZ L. 250 T1, HOJLUND 220 T2, KEAN 180 T3, "
    "CASTRO S. 140 T4, DIAO 100 T5, LUCCA 80 T6, PINAMONTI 40 T7"
)
SCHEMA_KEYS = {
    "schema_version",
    "seed",
    "cfg",
    "snapshot_hash",
    "feasibility",
    "league",
    "per_player",
    "per_team",
    "team_player_prob",
    "competition",
    "warnings",
}


def _run_cli(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO),
        timeout=120,
        env=_cli_env(),
        check=False,  # il chiamante ispeziona il returncode
    )


def _listone_snapshot(tmp_path: Path) -> Path:
    """Snapshot 'fase finale' dal listone reale via auction live + vendite."""

    try:
        from openfanta.core.auction import (  # pyright: ignore[reportMissingImports]
            Auction,
            load_players,
        )
    except ImportError as e:
        pytest.skip(f"motore live non importabile (WIP altri WP): {e}")
    players = load_players(str(LISTONE))
    auction = Auction(players, teams=8, budget=500)
    for part in SALES.split(","):
        if not part.strip():
            continue
        toks = part.split()
        name, price, team = " ".join(toks[:-2]), int(toks[-2]), toks[-1]
        p = auction.find(name)
        if p is None:
            raise AssertionError(f"giocatore non trovato nel listone: {name}")
        auction.mark_sold(p, price, team)
    snapshot = snapshot_from_auction(auction)
    out = tmp_path / "snapshot.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return out


def test_acceptance_10k_wall_time_and_schema(tmp_path):
    snap = _listone_snapshot(tmp_path)
    out1 = tmp_path / "report1.json"
    t0 = time.monotonic()
    r = _run_cli(
        [
            "--snapshot",
            str(snap),
            "--runs",
            "10000",
            "--seed",
            "42",
            "--deterministic-report",
            "--json-out",
            str(out1),
            "--no-cache",
        ]
    )
    wall = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert wall < 45.0, f"performance fuori bound: {wall:.1f}s"
    data = json.loads(out1.read_text(encoding="utf-8"))
    assert set(data) >= SCHEMA_KEYS
    assert data["seed"] == 42
    assert data["cfg"]["n_runs"] == 10000
    assert len(data["per_player"]) > 0
    assert len(data["per_team"]) == 8
    assert "generated_at" not in data


def test_acceptance_byte_identical(tmp_path):
    snap = _listone_snapshot(tmp_path)
    out1 = tmp_path / "a.json"
    out2 = tmp_path / "b.json"
    for out in (out1, out2):
        r = _run_cli(
            [
                "--snapshot",
                str(snap),
                "--runs",
                "5000",
                "--seed",
                "7",
                "--deterministic-report",
                "--json-out",
                str(out),
                "--no-cache",
            ]
        )
        assert r.returncode == 0, r.stderr
    assert out1.read_bytes() == out2.read_bytes()


def test_acceptance_invalid_snapshot_exit2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    r = _run_cli(["--snapshot", str(bad), "--runs", "100"])
    assert r.returncode == 2
    assert r.stdout.strip() == ""
    assert r.stderr != ""
    # snapshot valido ma schema errato
    bad2 = tmp_path / "bad2.json"
    bad2.write_text(
        json.dumps({"schema_version": 1, "teams": [], "players": [], "pool": []}),
        encoding="utf-8",
    )
    r2 = _run_cli(["--snapshot", str(bad2), "--runs", "100"])
    assert r2.returncode == 2
    assert r2.stdout.strip() == ""
    # config invalida -> exit 2
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "teams": [
                    {"team": "IO", "budget": 100, "slots_a": 1, "slots_other": {}}
                ],
                "players": [
                    {
                        "pid": "p1",
                        "nome": "X",
                        "base": 30,
                        "pma": 31.0,
                        "unc_pfc": 2.0,
                        "suggested0": 33.0,
                        "scarc0": 1.0,
                        "unsold0": 0,
                        "value": None,
                        "rank": None,
                    }
                ],
                "pool": ["p1"],
                "money_league": 100,
            }
        ),
        encoding="utf-8",
    )
    r3 = _run_cli(["--snapshot", str(good), "--runs", "0"])
    assert r3.returncode == 2


def test_acceptance_cache_content_addressed(tmp_path):
    snap = _listone_snapshot(tmp_path)
    cache_dir = tmp_path / "cache"
    common = [
        "--snapshot",
        str(snap),
        "--runs",
        "2000",
        "--seed",
        "11",
        "--cache-dir",
        str(cache_dir),
    ]
    r1 = _run_cli([*common, "--json-out", str(tmp_path / "c1.json")])
    assert r1.returncode == 0, r1.stderr
    assert "calcolo" in r1.stderr
    cache_files = sorted(cache_dir.glob("*.json"))
    assert len(cache_files) == 1  # una sola chiave (content-addressed)
    # seconda run stessa chiave: cache hit (stesso payload byte-identico)
    r2 = _run_cli([*common, "--json-out", str(tmp_path / "c2.json")])
    assert r2.returncode == 0
    assert "cache" in r2.stderr
    assert (tmp_path / "c1.json").read_bytes() == (tmp_path / "c2.json").read_bytes()
    assert sorted(cache_dir.glob("*.json")) == cache_files  # chiave invariata
    # snapshot modificato -> chiave nuova -> ricalcolo
    mod = tmp_path / "snapshot_mod.json"
    snap_data = json.loads(snap.read_text(encoding="utf-8"))
    snap_data["teams"][0]["budget"] += 30
    mod.write_text(json.dumps(snap_data), encoding="utf-8")
    r3 = _run_cli(
        [
            "--snapshot",
            str(mod),
            "--runs",
            "2000",
            "--seed",
            "11",
            "--cache-dir",
            str(cache_dir),
            "--json-out",
            str(tmp_path / "c3.json"),
        ]
    )
    assert r3.returncode == 0
    assert "calcolo" in r3.stderr
    assert (tmp_path / "c1.json").read_bytes() != (tmp_path / "c3.json").read_bytes()


def test_acceptance_snapshot_subcommand(tmp_path):
    if not LISTONE.exists():
        pytest.skip("listone reale assente")
    out = tmp_path / "snap.json"
    r = _run_cli(
        [
            "snapshot",
            "--csv",
            str(LISTONE),
            "--squadre",
            "4",
            "--budget",
            "300",
            "--sales",
            "MALEN 120 IO, DIAO 90 T1",
            "--out",
            str(out),
        ]
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert len(data["teams"]) == 4
    names = {p["nome"] for p in data["players"]}
    # MALEN venduto -> fuori dal pool: assente dai giocatori dello snapshot
    assert "MALEN" not in names
    assert "MALEN" not in {p["nome"] for p in data["players"]}
    assert set(data["pool"]) <= {p["pid"] for p in data["players"]}
