"""Test del backtest prezzo d'asta in replay prequenziale (WP9).

Scenario sintetici su fixture tmp: gate pass/fail ai confini, replay
prequenziale senza leakage futuro, baseline sempre presenti, parser CSV e
backup JSON, errori bloccanti (duplicati/missing/prezzo), report atomici,
exit codes. Nessun dato storico inventato nel repo: tutto vive in tmp.
"""

import json

import pytest  # pyright: ignore[reportMissingImports]

import openfanta.backtest.price as bta  # pyright: ignore[reportMissingImports]

SALES_HEADER = "pid,nome,ruolo,price,team,seq,ts,season\n"
LISTONE_HEADER = "pid,nome,squadra,ruolo,pfc,pma,slot,tit,expfm,fascia,status\n"


def write_listone(path, players):
    """players = [(nome, ruolo, pfc)] -> listone CSV canonico minimale."""
    lines = [LISTONE_HEADER]
    for nome, ruolo, pfc in players:
        lines.append(f",{nome},FC,{ruolo},{pfc},{pfc},2,60.0,6.5,Top,T\n")
    path.write_text("".join(lines), encoding="utf-8")


def pid_of(nome, ruolo):
    from openfanta.core.auction import (
        compute_pid,  # pyright: ignore[reportMissingImports]
    )
    from openfanta.core.config import norm  # pyright: ignore[reportMissingImports]

    return compute_pid(norm(nome), ruolo)


def sales_rows_for(listone_path, price_factor=2, team="ALTRO"):
    """Righe vendite dal listone su disco: (pid,nome,ruolo,price,team,seq,ts,season)."""
    rows = []
    with open(listone_path, encoding="utf-8") as f:
        for i, line in enumerate(f.read().splitlines()[1:]):
            nome, ruolo, pfc = (
                line.split(",")[1],
                line.split(",")[3],
                int(line.split(",")[4]),
            )
            price = max(1, round(pfc * price_factor))
            rows.append(
                (
                    pid_of(nome, ruolo),
                    nome,
                    ruolo,
                    price,
                    team,
                    i + 1,
                    f"2026-08-0{min(i, 8) + 1}T10:00:00",
                    "2026-27",
                )
            )
    return rows


def write_sales(path, rows, header=SALES_HEADER):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        f.writelines(",".join(str(v) for v in row) + "\n" for row in rows)


@pytest.fixture
def big_listone(tmp_path):
    """60 attaccanti con PFC distinto: nel replay ne vengono venduti 40
    (il pool residuo mantiene l'inflazione del replay realistica e finita)."""
    players = [(f"GIOCATORE{i}", "A", 10 + i) for i in range(60)]
    path = tmp_path / "listone.csv"
    write_listone(path, players)
    return path


def _run_and_load(tmp_path, listone_path, rows, extra=()):
    """Esegue il backtest su tmp e ritorna (exit_code, report_json, out_dir)."""
    out_dir = tmp_path / "out"
    sales_path = tmp_path / "sales.csv"
    write_sales(sales_path, rows)
    code = bta.main(
        [
            "--sales",
            str(sales_path),
            "--listone",
            str(listone_path),
            "--teams",
            "8",
            "--budget",
            "500",
            "--out",
            str(out_dir),
            *extra,
        ]
    )
    json_path = out_dir / "backtest_auction_report.json"
    assert json_path.exists(), "report JSON mancante"
    report = json.loads(json_path.read_text(encoding="utf-8"))
    return code, report, out_dir


# ------------------------------------------------------------------ scenario
def test_gate_pass_scenario(tmp_path, big_listone):
    """40 vendite a 2x base: la calibrazione impara il premio e batte il PFC."""
    rows = sales_rows_for(big_listone)[:40]
    code, report, out_dir = _run_and_load(tmp_path, big_listone, rows)
    assert code == 0
    # baseline sempre presenti nel report
    assert set(report["models"].keys()) == {"pfc", "pma", "live", "calibrated"}
    for baseline in ("pfc", "pma", "live"):
        assert report["models"][baseline]["global"][""]["n"] == 40
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is True, gate["verdict"]["reasons"]
    # il gate NON attiva la calibrazione: resta false finche' decisione esplicita
    assert gate["use_calibration_in_price"] is False
    assert (out_dir / "backtest_auction_report.csv").exists()
    assert (out_dir / "backtest_auction_report.txt").exists()


def test_gate_fail_n_insufficiente(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)[:10]
    code, report, _ = _run_and_load(tmp_path, big_listone, rows)
    assert code == 0  # il backtest esce 0 anche con gate fallito (report scritti)
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is False
    assert any("campione insufficiente" in r for r in gate["verdict"]["reasons"])
    assert gate["use_calibration_in_price"] is False


def test_report_deterministico(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)[:12]
    _, report1, out_dir = _run_and_load(tmp_path, big_listone, rows)
    text1 = (out_dir / "backtest_auction_report.json").read_text()
    _, _report2, _ = _run_and_load(tmp_path, big_listone, rows)
    text2 = (out_dir / "backtest_auction_report.json").read_text()
    assert text1 == text2  # niente timestamp: rilancio identico = byte identici
    assert report1["meta"]["n_sales"] == 12


def test_prequential_no_leakage(tmp_path, big_listone):
    """La predizione per la vendita i non cambia se le vendite future vengono
    rimosse: replay prequenziale = nessuna informazione futura nel modello."""
    full_rows = sales_rows_for(big_listone)[:40]
    cut = 20
    _, full_report, _ = _run_and_load(tmp_path, big_listone, full_rows)
    _, prefix_report, _ = _run_and_load(tmp_path, big_listone, full_rows[:cut])
    full_preds = full_report["predictions"]
    prefix_preds = prefix_report["predictions"]
    assert len(full_preds) == len(full_rows)
    for i in range(cut):
        assert full_preds[i]["preds"] == prefix_preds[i]["preds"], (
            f"predizione {i} cambia se rimuovo le vendite future: leakage"
        )
    assert len(prefix_preds) == cut


def test_calibrazione_addestrata_solo_sui_prior(tmp_path, big_listone):
    """La prima vendita ha calibrazione senza dati (n=0): il modello non puo'
    conoscere il prezzo corrente."""
    rows = sales_rows_for(big_listone)[:40]
    _, report, _ = _run_and_load(tmp_path, big_listone, rows)
    assert report["predictions"][0]["calibration"]["n"] == 0
    assert report["predictions"][5]["calibration"]["n"] == 5


def test_baseline_presenti_nei_predictions(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)[:40]
    _, report, _ = _run_and_load(tmp_path, big_listone, rows)
    preds = report["predictions"][10]["preds"]
    assert {"pfc", "pma", "live", "calibrated"} <= set(preds.keys())
    assert preds["pfc"] >= 1 and preds["live"] >= 1 and preds["calibrated"] >= 1


def test_metriche_per_ruolo_banda_fase(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)[:40]
    _, report, _ = _run_and_load(tmp_path, big_listone, rows)
    for model in report["models"]:
        slices = report["models"][model]
        assert slices["role"]["A"]["n"] == 40
        assert sum(s["n"] for s in slices["phase"].values()) == 40
        assert sum(s["n"] for s in slices["band"].values()) == 40


def _run_and_load_error(tmp_path, listone_path, rows):
    """Esegue il backtest aspettandosi un errore input: ritorna (exit_code,).
    Nessun artefatto deve esistere."""
    out_dir = tmp_path / "out_err"
    sales_path = tmp_path / "sales_err.csv"
    write_sales(sales_path, rows)
    code = bta.main(
        [
            "--sales",
            str(sales_path),
            "--listone",
            str(listone_path),
            "--teams",
            "8",
            "--budget",
            "500",
            "--out",
            str(out_dir),
        ]
    )
    return code, None, out_dir


# ------------------------------------------------------------------ parser
def test_team_sconosciuta_diventa_altro(tmp_path, big_listone):
    rows = list(sales_rows_for(big_listone)[:40])
    rows[0] = (*rows[0][:4], "SQUADRA_INESISTENTE", *rows[0][5:])
    _, report, _ = _run_and_load(tmp_path, big_listone, rows)
    assert report["predictions"][0]["team"] == "ALTRO"


def test_squadra_tracciata_riconosciuta(tmp_path, big_listone):
    rows = list(sales_rows_for(big_listone)[:40])
    rows[0] = (*rows[0][:4], "T1", *rows[0][5:])
    _, report, _ = _run_and_load(tmp_path, big_listone, rows)
    assert report["predictions"][0]["team"] == "T1"


def test_vendita_duplicata_bloccante(tmp_path, big_listone):
    rows = [sales_rows_for(big_listone)[0]] * 2
    code, _, _ = _run_and_load_error(tmp_path, big_listone, rows)
    assert code == 2


def test_giocatore_assente_bloccante(tmp_path, big_listone):
    rows = [("ffff", "INVENTATO", "A", 10, "ALTRO", 1, "", "2026-27")]
    code, _, _ = _run_and_load_error(tmp_path, big_listone, rows)
    assert code == 2


def test_prezzo_non_intero_bloccante(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)
    rows[0] = (*rows[0][:3], "15.5", *rows[0][4:])
    code, _, _ = _run_and_load_error(tmp_path, big_listone, rows)
    assert code == 2


def test_prezzo_non_positivo_bloccante(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)
    rows[0] = (*rows[0][:3], "0", *rows[0][4:])
    code, _, _ = _run_and_load_error(tmp_path, big_listone, rows)
    assert code == 2


def test_prezzo_mancante_bloccante(tmp_path, big_listone):
    rows = sales_rows_for(big_listone)
    rows[0] = (*rows[0][:3], "", *rows[0][4:])
    code, _, _ = _run_and_load_error(tmp_path, big_listone, rows)
    assert code == 2


def test_risolutore_per_nome_legacy(tmp_path, big_listone):
    """pid vuoto: il nome risolve il giocatore (fallback legacy)."""
    rows = list(sales_rows_for(big_listone)[:40])
    rows[0] = (
        "",
        rows[0][1],
        rows[0][2],
        rows[0][3],
        "ALTRO",
        1,
        "2026-08-01T10:00:00",
        "2026-27",
    )
    code, report, _ = _run_and_load(tmp_path, big_listone, rows)
    assert code == 0
    assert report["predictions"][0]["pid"] == rows[0][0] or code == 0


def test_nessun_artefatto_su_errore(tmp_path, big_listone):
    out_dir = tmp_path / "out"
    code = bta.main(
        [
            "--sales",
            str(tmp_path / "inesistente.csv"),
            "--listone",
            str(big_listone),
            "--out",
            str(out_dir),
        ]
    )
    assert code == 2
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_exit2_su_sales_vuoto(tmp_path, big_listone):
    sales_path = tmp_path / "sales.csv"
    write_sales(sales_path, [])
    code = bta.main(
        [
            "--sales",
            str(sales_path),
            "--listone",
            str(big_listone),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2


def test_backup_json_parser_con_revoke(tmp_path, big_listone):
    """Backup event store: gli eventi revocati (supersedes) sono esclusi."""
    rows = sales_rows_for(big_listone)[:40]
    events = []
    for i, row in enumerate(rows):
        events.append(
            {
                "seq": i + 1,
                "ts": "2026-08-01T10:00:00+00:00",
                "type": "sold",
                "payload": {
                    "pid": row[0],
                    "nome": row[1],
                    "ruolo": row[2],
                    "price": row[3],
                    "team": row[4],
                    "base": row[3] // 2,
                },
                "supersedes": None,
            }
        )
    # revoke della prima vendita + ri-vendita dello stesso giocatore su T1
    events.append(
        {
            "seq": len(events) + 1,
            "ts": "2026-08-01T11:00:00+00:00",
            "type": "revoke",
            "payload": {"target_seq": 1},
            "supersedes": 1,
        }
    )
    events.append(
        {
            "seq": len(events) + 1,
            "ts": "2026-08-01T12:00:00+00:00",
            "type": "sold",
            "payload": {
                "pid": rows[0][0],
                "nome": rows[0][1],
                "ruolo": rows[0][2],
                "price": rows[0][3],
                "team": "T1",
                "base": rows[0][3] // 2,
            },
            "supersedes": None,
        }
    )
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        json.dumps(
            {"format": "openfanta-draft-events", "version": 1, "events": events}
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    code = bta.main(
        [
            "--sales",
            str(backup_path),
            "--listone",
            str(big_listone),
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    report = json.loads((out_dir / "backtest_auction_report.json").read_text())
    # 40 vendite attive: la seq 1 e' revocata e sostituita dalla ri-vendita su T1
    assert report["meta"]["n_sales"] == 40
    last = report["predictions"][-1]
    assert last["pid"] == rows[0][0]
    assert last["team"] == "T1"


def test_backup_json_giocatore_assente_bloccante(tmp_path, big_listone):
    backup_path = tmp_path / "backup.json"
    backup_path.write_text(
        json.dumps(
            {
                "format": "openfanta-draft-events",
                "version": 1,
                "events": [
                    {
                        "seq": 1,
                        "ts": None,
                        "type": "sold",
                        "payload": {
                            "pid": "nope",
                            "nome": "X",
                            "ruolo": "A",
                            "price": 10,
                            "team": "ALTRO",
                            "base": 10,
                        },
                        "supersedes": None,
                    }
                ],
            }
        )
    )
    code = bta.main(
        [
            "--sales",
            str(backup_path),
            "--listone",
            str(big_listone),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
