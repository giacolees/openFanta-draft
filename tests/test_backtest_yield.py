"""Test del backtest rendimento stagionale separato dal prezzo (WP9).

Fixture tmp: join vendite x outcomes, yield per team/ruolo/banda, baseline
PFC/PMA dichiarate, proxy expfm etichettato con gate false, stessa stagione
= gate false, stagioni distinte + n sufficiente = gate puo' passare.
"""

import json

import pytest  # pyright: ignore[reportMissingImports]

import openfanta.backtest.yield_report as bty  # pyright: ignore[reportMissingImports]

SALES_HEADER = "pid,nome,ruolo,price,team,seq,ts,season\n"
LISTONE_HEADER = "pid,nome,squadra,ruolo,pfc,pma,slot,tit,expfm,fascia,status\n"
SEASON_SALE = "2026-27"
SEASON_PREV = "2025-26"


def pid_of(nome, ruolo):
    from openfanta.core.auction import (
        compute_pid,  # pyright: ignore[reportMissingImports]
    )
    from openfanta.core.config import norm  # pyright: ignore[reportMissingImports]

    return compute_pid(norm(nome), ruolo)


def write_listone(path, players):
    lines = [LISTONE_HEADER]
    for nome, ruolo, pfc, expfm in players:
        lines.append(f",{nome},FC,{ruolo},{pfc},{pfc},2,60.0,{expfm},Top,T\n")
    path.write_text("".join(lines), encoding="utf-8")


def write_sales(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(SALES_HEADER)
        f.writelines(",".join(str(v) for v in row) + "\n" for row in rows)


@pytest.fixture
def fixture_std(tmp_path):
    """40 attaccanti venduti; realized_fm perfettamente correlato al PFC."""
    n = 40
    players = [(f"GIOCATORE{i}", "A", 10 + i, 6.0 + i * 0.01) for i in range(n)]
    listone = tmp_path / "listone.csv"
    write_listone(listone, players)
    rows = []
    for i in range(n):
        nome = players[i][0]
        pfc = 10 + i
        rows.append(
            (
                pid_of(nome, "A"),
                nome,
                "A",
                pfc * 2,
                "T1",
                i + 1,
                f"2026-08-0{min(i, 8) + 1}T10:00:00",
                SEASON_SALE,
            )
        )
    sales = tmp_path / "sales.csv"
    write_sales(sales, rows)
    return {
        "listone": listone,
        "sales": sales,
        "rows": rows,
        "n": n,
        "tmp": tmp_path,
    }


def write_outcomes(path, rows, season, realized_of):
    """righe outcome: pid,nome,realized_fm,minutes,season"""
    lines = ["pid,nome,realized_fm,minutes,season\n"]
    for row in rows:
        lines.append(f"{row[0]},{row[1]},{realized_of(row)},90,{season}\n")
    path.write_text("".join(lines), encoding="utf-8")


def run_yield(
    tmp,
    fixture,
    season_outcome,
    realized_of=None,
    sales_path=None,
    outcomes_path=None,
    extra=(),
):
    if realized_of is None:
        realized_of = lambda row: row[3] / 20.0  # price = 2*pfc -> realized = pfc/10
    if outcomes_path is None:
        outcomes_path = tmp / "outcomes.csv"
        write_outcomes(outcomes_path, fixture["rows"], season_outcome, realized_of)
    out_dir = tmp / "out"
    code = bty.main(
        [
            "--sales",
            str(sales_path or fixture["sales"]),
            "--outcomes",
            str(outcomes_path),
            "--listone",
            str(fixture["listone"]),
            "--teams",
            "8",
            "--out",
            str(out_dir),
            *extra,
        ]
    )
    report = None
    json_path = out_dir / "backtest_yield_report.json"
    if json_path.exists():
        report = json.loads(json_path.read_text(encoding="utf-8"))
    return code, report, out_dir


# ------------------------------------------------------------------ join
def test_join_e_yield_per_team_ruolo_banda(fixture_std):
    code, report, _ = run_yield(fixture_std["tmp"], fixture_std, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    assert report["meta"]["n_joined"] == 40
    assert report["meta"]["target"] == "realized_fm"
    # baseline dichiarate
    assert report["meta"]["baselines"] == ["PFC", "PMA"]
    # yield globale = somma realized / somma prezzi = (pfc/10) / (pfc*2) = 0.05
    glob = report["yield"]["global"]["globale"]
    assert glob["sum_price"] == sum(r[3] for r in fixture_std["rows"])
    assert glob["yield"] == pytest.approx(0.05, rel=1e-3)
    # yield per team: T1 riceve tutte le 40 vendite
    assert report["yield"]["team"]["T1"]["n"] == 40
    # yield per ruolo
    assert report["yield"]["role"]["A"]["n"] == 40
    # yield per banda: almeno una banda non vuota
    assert sum(b["n"] for b in report["yield"]["band"].values()) == 40


def test_ranking_baseline_pfc_pma_expfm(fixture_std):
    code, report, _ = run_yield(fixture_std["tmp"], fixture_std, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    ranking = report["ranking_vs_outcome"]
    assert set(ranking.keys()) == {"pfc", "pma", "expfm"}
    # realized = pfc/10: Spearman PFC vs realized perfetto
    assert ranking["pfc"]["global"][""]["spearman"] == pytest.approx(1.0)
    assert ranking["pma"]["global"][""]["spearman"] == pytest.approx(1.0)
    # MAE computabile solo per expfm (stessa scala fantamedia)
    assert ranking["pfc"]["global"][""]["mae"] is None
    assert ranking["expfm"]["global"][""]["mae"] is not None


def test_gate_pass_con_stagioni_distinte(fixture_std):
    code, report, _ = run_yield(fixture_std["tmp"], fixture_std, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    gate = report["gate_recommendation"]
    # n=40 >= 30, Spearman=1, stagioni dichiarate e distinte, no proxy
    assert gate["verdict"]["passed"] is True, gate["verdict"]["reasons"]
    assert gate["use_calibration_in_price"] is False
    assert gate["target"] == "realized_fm"


def test_gate_false_stessa_stagione(fixture_std):
    code, report, _ = run_yield(fixture_std["tmp"], fixture_std, SEASON_SALE)
    assert report is not None
    assert code == 0
    assert report is not None
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is False
    assert any("stessa stagione" in r for r in gate["verdict"]["reasons"])
    assert gate["use_calibration_in_price"] is False


def test_gate_false_proxy_expfm(fixture_std, tmp_path):
    """Outcomes senza alcuna colonna di rendimento: target proxy, gate false."""
    outcomes = tmp_path / "outcomes_proxy.csv"
    lines = ["pid,nome,minutes,season\n"]
    for row in fixture_std["rows"]:
        lines.append(f"{row[0]},{row[1]},90,{SEASON_PREV}\n")
    outcomes.write_text("".join(lines), encoding="utf-8")
    code, report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=outcomes
    )
    assert code == 0
    assert report is not None
    gate = report["gate_recommendation"]
    assert gate["target"] == "proxy_expfm"
    assert gate["verdict"]["passed"] is False
    assert any("proxy" in r for r in gate["verdict"]["reasons"])
    assert report["meta"]["proxy_note"]


def test_gate_false_stagione_vendite_non_dichiarata(fixture_std, tmp_path):
    """Vendite senza stagione: out-of-sample non verificabile -> gate false."""
    rows = [tuple(list(r)[:7] + [""]) for r in fixture_std["rows"]]
    sales = tmp_path / "sales_noseason.csv"
    write_sales(sales, rows)
    fixture = {**fixture_std, "sales": sales}
    code, report, _ = run_yield(tmp_path, fixture, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is False
    assert any("non dichiarata" in r for r in gate["verdict"]["reasons"])


def test_gate_fail_campione_insufficiente(tmp_path):
    """Solo 10 vendite: n < 30 -> gate false anche con Spearman perfetto."""
    n = 10
    players = [(f"G{i}", "A", 10 + i, 6.0) for i in range(n)]
    listone = tmp_path / "listone.csv"
    write_listone(listone, players)
    rows = []
    for i in range(n):
        rows.append(
            (
                pid_of(players[i][0], "A"),
                players[i][0],
                "A",
                (10 + i) * 2,
                "T1",
                i + 1,
                "2026-08-01T10:00:00",
                SEASON_SALE,
            )
        )
    sales = tmp_path / "sales.csv"
    write_sales(sales, rows)
    fixture = {
        "listone": listone,
        "sales": sales,
        "rows": rows,
        "n": n,
        "tmp": tmp_path,
    }
    code, report, _ = run_yield(tmp_path, fixture, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is False
    assert any("campione insufficiente" in r for r in gate["verdict"]["reasons"])


# ------------------------------------------------------------------ parser
def test_outcomes_nome_fallback_legacy(tmp_path):
    """Outcomes senza pid: il nome normalizzato risolve il giocatore."""
    players = [("ROSSI MARIO", "A", 50, 7.0)]
    listone = tmp_path / "listone.csv"
    write_listone(listone, players)
    rows = [
        (
            pid_of("ROSSI MARIO", "A"),
            "ROSSI MARIO",
            "A",
            100,
            "T1",
            1,
            "2026-08-01T10:00:00",
            SEASON_SALE,
        )
    ]
    sales = tmp_path / "sales.csv"
    write_sales(sales, rows)
    outcomes = tmp_path / "outcomes.csv"
    outcomes.write_text(
        f"pid,nome,realized_fm,minutes,season\n,rossi mario,7.0,90,{SEASON_PREV}\n",
        encoding="utf-8",
    )
    fixture = {
        "listone": listone,
        "sales": sales,
        "rows": rows,
        "n": 1,
        "tmp": tmp_path,
    }
    code, report, _ = run_yield(tmp_path, fixture, SEASON_PREV)
    assert report is not None
    assert code == 0
    assert report is not None
    assert report["meta"]["n_joined"] == 1


def test_outcomes_giocatore_assente_bloccante(fixture_std, tmp_path):
    outcomes = tmp_path / "outcomes.csv"
    outcomes.write_text(
        f"pid,nome,realized_fm,minutes,season\nnope,X,7.0,90,{SEASON_PREV}\n",
        encoding="utf-8",
    )
    code, report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=outcomes
    )
    assert code == 2
    assert report is None


def test_outcomes_realized_non_numerico_bloccante(fixture_std, tmp_path):
    outcomes = tmp_path / "outcomes.csv"
    lines = [
        f"pid,nome,realized_fm,minutes,season\n{fixture_std['rows'][0][0]},G0,alto,90,{SEASON_PREV}\n"
    ]
    outcomes.write_text("".join(lines), encoding="utf-8")
    code, _report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=outcomes
    )
    assert code == 2


def test_exit2_outcomes_vuoti(fixture_std, tmp_path):
    outcomes = tmp_path / "outcomes.csv"
    outcomes.write_text("pid,nome,realized_fm,minutes,season\n", encoding="utf-8")
    code, report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=outcomes
    )
    assert code == 2
    assert report is None


def test_exit2_file_outcomes_mancante(fixture_std, tmp_path):
    code, report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=tmp_path / "nope.csv"
    )
    assert code == 2
    assert report is None


def test_nessun_join_exit2(fixture_std, tmp_path):
    """Vendite senza stagione e outcomes duplicati in piu' stagioni: join
    non risolvibile -> exit 2 senza artefatti."""
    outcomes = tmp_path / "outcomes_multi.csv"
    lines = ["pid,nome,realized_fm,minutes,season\n"]
    for row in fixture_std["rows"]:
        lines.append(f"{row[0]},{row[1]},7.0,90,{SEASON_PREV}\n")
        lines.append(f"{row[0]},{row[1]},6.5,90,{SEASON_SALE}\n")
    outcomes.write_text("".join(lines), encoding="utf-8")
    sales = tmp_path / "sales_noseason.csv"
    write_sales(sales, [tuple(list(r)[:7] + [""]) for r in fixture_std["rows"]])
    fixture = {**fixture_std, "sales": sales}
    code, report, _ = run_yield(tmp_path, fixture, SEASON_PREV, outcomes_path=outcomes)
    assert code == 2
    assert report is None


def test_fallback_stagione_diversa_un_outcome(fixture_std):
    """Outcomes di stagione diversa dalla vendita: join retrospettivo accettato."""
    code, report, _ = run_yield(fixture_std["tmp"], fixture_std, "2010-11")
    assert code == 0
    assert report is not None
    assert report["meta"]["n_joined"] == 40
    # il gate valuta comunque: stagioni dichiarate e distinte -> puo' passare
    gate = report["gate_recommendation"]
    assert gate["verdict"]["passed"] is True, gate["verdict"]["reasons"]


def test_report_deterministico_csv_txt(fixture_std):
    code, _, out_dir = run_yield(fixture_std["tmp"], fixture_std, SEASON_PREV)
    assert code == 0
    text1 = (out_dir / "backtest_yield_report.json").read_text()
    assert (out_dir / "backtest_yield_report.csv").exists()
    assert (out_dir / "backtest_yield_report.txt").exists()
    code2, _, _ = run_yield(fixture_std["tmp"], fixture_std, SEASON_PREV)
    text2 = (out_dir / "backtest_yield_report.json").read_text()
    assert text1 == text2
    assert code2 == 0


def test_alias_points_per_realized(fixture_std, tmp_path):
    """La colonna 'points' e' accettata al posto di 'realized_fm'."""
    outcomes = tmp_path / "outcomes.csv"
    lines = ["pid,nome,points,minutes,season\n"]
    for row in fixture_std["rows"]:
        lines.append(f"{row[0]},{row[1]},{row[3] / 10.0},90,{SEASON_PREV}\n")
    outcomes.write_text("".join(lines), encoding="utf-8")
    code, report, _ = run_yield(
        tmp_path, fixture_std, SEASON_PREV, outcomes_path=outcomes
    )
    assert code == 0
    assert report is not None
    assert report["meta"]["target"] == "realized_fm"
