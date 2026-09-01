"""Test dell'import del listone (WP3 — scripts/import_listone.py).

Coprono, su un workbook sintetico generato con openpyxl in ``tmp_path``:
- happy path: colonne nuove (pid, pfc_lo/hi, pma_lo/hi, unc_pfc), meta sidecar
  (season, source_file, imported_at timezone-aware, conteggi/PFC per ruolo,
  colonne, algoritmo/versione pid), pids STABILI al re-import;
- identita': il pid NON cambia al cambio squadra (sopravvive ai trasferimenti);
- header mancante o dato obbligatorio invalido => errore BLOCCANTE, nessun CSV
  scritto, e l'ultimo CSV/meta validi restano INTATTI (atomicita');
- omonimi reali nello stesso ruolo => collisione di pid BLOCCANTE (mai merge,
  mai suffissi inventati); riga duplicata => bloccante;
- range sporco => warning + None (mai dati inventati); pma non positivo =>
  warning + stima=pfc (non blocca: 3 giocatori reali del listone hanno pma=0);
- backward-compat di ``load_players``: CSV legacy senza pid/range numerici =>
  pid derivato con lo stesso algoritmo e range parse dai range stringa; pfc
  malformato => ValueError contestualizzato;
- lookup del motore: per pid e per nome legacy, AmbiguousName sugli omonimi,
  collisione di identita' al costruttore;
- contratti API/export: key=pid, eventi con pid, colonna Pid negli export;
- CLI: exit 2 su file invalido, 0 su file valido (report JSON scritto).
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import import_listone as il  # pyright: ignore[reportMissingImports]
import openpyxl  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import make_player  # pyright: ignore[reportMissingImports]
from import_listone import compute_pid  # pyright: ignore[reportMissingImports]
from live_auction import (  # pyright: ignore[reportMissingImports]
    AmbiguousName,
    Auction,
    ConfigError,
    load_players,
)

ROOT = Path(__file__).resolve().parent.parent

# intestazioni reali del foglio ALL (sottoinsieme minimo per l'import)
HEADER = [
    "name",
    "team",
    "role",
    "pma",
    "pfc",
    "dpfcpma",
    "pmaRange",
    "pfcRange",
    "slot",
    "expectedTitolarita",
    "expectedFantamedia",
    "playerStatus",
    "penaltyProbability",
    "freeKickProbability",
    "fasciaFc",
]

FIELD_IDX = {
    "name": 0,
    "team": 1,
    "role": 2,
    "pma": 3,
    "pfc": 4,
    "dpfcpma": 5,
    "pmaRange": 6,
    "pfcRange": 7,
    "slot": 8,
    "tit": 9,
    "expfm": 10,
    "status": 11,
    "fascia": 14,
}


def make_row(
    nome,
    team,
    ruolo,
    pfc,
    pma=None,
    pfc_range=None,
    pma_range=None,
    slot=2,
    tit=60.0,
    expfm=6.4,
    status="T",
    fascia="Top",
):
    pma = pma if pma is not None else pfc
    pfc_range = pfc_range or f"{int(pfc * 0.9)}-{int(pfc * 1.1)}"
    pma_range = pma_range or f"{int(pma * 0.9)}-{int(pma * 1.1)}"
    return [
        nome,
        team,
        ruolo,
        pma,
        pfc,
        round(pfc - pma, 1),
        pma_range,
        pfc_range,
        slot,
        tit,
        expfm,
        status,
        0,
        0,
        fascia,
    ]


def write_workbook(path, rows, header=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "ALL"
    ws.append(header or HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)


# ---------------------------------------------------------------- happy path
def test_happy_path_colonne_meta_e_pid_stabili(tmp_path):
    xlsx = tmp_path / "listone.xlsx"
    rows = [
        make_row("MARCO R.", "Milano", "A", 60.0, pfc_range="54-66"),
        make_row("LUCA D.", "Roma", "D", 40.0),
        make_row("PAOLO", "Torino", "C", 30.0),
    ]
    write_workbook(xlsx, rows)
    out = tmp_path / "out.csv"
    meta = tmp_path / "meta.json"

    r1 = il.import_listone(str(xlsx), str(out), str(meta))
    assert r1["ok"] is True
    assert r1["errors"] == []
    assert r1["n_imported"] == 3
    assert r1["pid_collisions"] == []

    with open(out, encoding="utf-8-sig", newline="") as f:
        rd = list(csv.DictReader(f))
    assert len(rd) == 3
    # colonne nuove WP3 presenti e corrette
    for col in ("pid", "pfc_lo", "pfc_hi", "pma_lo", "pma_hi", "unc_pfc"):
        assert col in rd[0], col
    assert rd[0]["pid"] == compute_pid("MARCO R.", "A")
    assert rd[0]["pfc_range"] == "54-66"  # stringhe dei range preservate
    assert rd[0]["pfc_lo"] == "54.0" and rd[0]["pfc_hi"] == "66.0"
    assert rd[0]["unc_pfc"] == "6.0"  # semiampiezza = (66-54)/2

    # meta sidecar: stagione, fonte, data timezone-aware, conteggi, algoritmo
    with open(meta, encoding="utf-8") as f:
        m = json.load(f)
    assert m["season"] == "2026-27"
    assert m["source_file"] == "listone.xlsx"
    assert m["imported_at"].endswith("+00:00")  # timezone-aware (UTC)
    assert m["n_players"] == 3
    assert m["n_by_role"] == {"P": 0, "D": 1, "C": 1, "A": 1}
    assert m["total_pfc_by_role"]["A"] == 60.0
    assert "pfc_lo" in m["columns"] and "pid" in m["columns"]
    assert m["pid_algorithm"] == "fnv1a64(norm(nome) + '|' + norm(ruolo))"
    assert m["pid_version"] == "1"

    # re-import dello stesso xlsx -> pids IDENTICI (stabilita' al refresh)
    r2 = il.import_listone(str(xlsx), str(out), str(meta))
    assert r2["ok"] is True
    with open(out, encoding="utf-8-sig", newline="") as f:
        rd2 = list(csv.DictReader(f))
    assert [row["pid"] for row in rd] == [row["pid"] for row in rd2]


def test_pid_stabile_al_cambio_squadra(tmp_path):
    """Il pid NON include la squadra: sopravvive ai trasferimenti tra refresh."""
    xlsx = tmp_path / "l.xlsx"
    write_workbook(xlsx, [make_row("MARCO R.", "Milano", "A", 60.0)])
    r1 = il.import_listone(
        str(xlsx), str(tmp_path / "o1.csv"), str(tmp_path / "m1.json")
    )
    pid1 = r1["players"][0]["pid"]

    # il giocatore si trasferisce: stesso nome+ruolo, squadra diversa
    write_workbook(xlsx, [make_row("MARCO R.", "Juventus", "A", 60.0)])
    r2 = il.import_listone(
        str(xlsx), str(tmp_path / "o2.csv"), str(tmp_path / "m2.json")
    )
    assert r2["players"][0]["squadra"] == "Juventus"
    assert r2["players"][0]["pid"] == pid1


# ----------------------------------------------------- header/dati invalidi
def test_header_mancante_blocca(tmp_path):
    header = [h for h in HEADER if h != "pfcRange"]
    write_workbook(
        tmp_path / "bad.xlsx",
        [make_row("MARCO R.", "Milano", "A", 60.0)],
        header=header,
    )
    out = tmp_path / "out.csv"
    r = il.import_listone(
        str(tmp_path / "bad.xlsx"), str(out), str(tmp_path / "m.json")
    )
    assert r["ok"] is False
    assert any(
        "header obbligatori mancanti" in e and "pfcRange" in e for e in r["errors"]
    )
    assert r["n_imported"] == 0
    assert not out.exists()  # nessun CSV scritto


@pytest.mark.parametrize(
    "field,value",
    [
        ("pfc", 0),  # pfc non positivo
        ("pfc", "abc"),  # pfc non numerico
        ("slot", 9),  # slot fuori 1..8
        ("slot", 0),
        ("tit", 150),  # tit fuori 0..100
        ("expfm", 11),  # expfm fuori 0..10
    ],
)
def test_dato_obbligatorio_invalido_blocca(tmp_path, field, value):
    """pma invalido NON e' in questa lista: non blocca (warning + stima pfc,
    vedi test_pma_non_positivo_warning_e_stima_pfc) — pfc/slot/tit/expfm si."""
    row = make_row("MARCO R.", "Milano", "A", 60.0)
    row[FIELD_IDX[field]] = value
    write_workbook(tmp_path / "bad.xlsx", [row])
    out = tmp_path / "out.csv"
    r = il.import_listone(
        str(tmp_path / "bad.xlsx"), str(out), str(tmp_path / "m.json")
    )
    assert r["ok"] is False, field
    assert not out.exists()
    assert r["n_imported"] == 0


def test_pma_non_positivo_warning_e_stima_pfc(tmp_path):
    """pma <= 0 non blocca ma avvisa e stima col pfc (media d'Italia mai obbligatoria:
    3 giocatori del listone reale 2026-09-01 hanno pma=0 e finiscono lo stesso)."""
    row = make_row("BALERDI", "Roma", "D", 40.0, pma=0)
    write_workbook(tmp_path / "l.xlsx", [row])
    r = il.import_listone(
        str(tmp_path / "l.xlsx"), str(tmp_path / "o.csv"), str(tmp_path / "m.json")
    )
    assert r["ok"] is True  # warning non bloccante
    assert any("pma" in w for w in r["warnings"])
    assert r["players"][0]["pma"] == 40.0  # stima = pfc, non dati inventati


# ------------------------------------------------------- duplicati/collisioni
def test_collisione_omonimi_stesso_ruolo_blocca(tmp_path):
    """Omonimi reali nello stesso ruolo: il pid collide -> errore bloccante con
    entrambi riportati; mai unione automatica ne' suffissi inventati."""
    rows = [
        make_row("ROSSI A.", "Roma", "D", 40.0),
        make_row("ROSSI A.", "Torino", "D", 30.0),
    ]
    write_workbook(tmp_path / "l.xlsx", rows)
    out = tmp_path / "o.csv"
    r = il.import_listone(str(tmp_path / "l.xlsx"), str(out), str(tmp_path / "m.json"))
    assert r["ok"] is False
    assert len(r["pid_collisions"]) == 1
    assert r["pid_collisions"][0]["pid"] == compute_pid("ROSSI A.", "D")
    assert len(r["pid_collisions"][0]["players"]) == 2
    err = next(e for e in r["errors"] if "collisione" in e)
    assert "ROSSI A." in err and "rossi a." in err
    # le due squadre (trasferimento/omonimo reale) sono riportate nei dettagli
    teams = {p["squadra"] for p in r["pid_collisions"][0]["players"]}
    assert teams == {"Roma", "Torino"}
    assert not out.exists()


def test_riga_duplicata_blocca(tmp_path):
    row = make_row("ROSSI A.", "Roma", "D", 40.0)
    write_workbook(tmp_path / "l.xlsx", [row, row])
    r = il.import_listone(
        str(tmp_path / "l.xlsx"), str(tmp_path / "o.csv"), str(tmp_path / "m.json")
    )
    assert r["ok"] is False
    assert any("duplicata" in e for e in r["errors"])


# ------------------------------------------------------------- atomicita'
def test_atomicita_output_precedente_intatto(tmp_path):
    """Un import bloccato non sovrascrive mai un CSV/meta validi esistenti."""
    write_workbook(tmp_path / "ok.xlsx", [make_row("MARCO R.", "Milano", "A", 60.0)])
    out = tmp_path / "listone.csv"
    meta = tmp_path / "meta.json"
    assert il.import_listone(str(tmp_path / "ok.xlsx"), str(out), str(meta))["ok"]
    before_csv = out.read_bytes()
    before_meta = meta.read_bytes()

    # import bloccato per header mancante
    header = [h for h in HEADER if h != "slot"]
    write_workbook(
        tmp_path / "bad.xlsx",
        [make_row("X Y.", "Milano", "A", 60.0)],
        header=header,
    )
    r = il.import_listone(str(tmp_path / "bad.xlsx"), str(out), str(meta))
    assert r["ok"] is False
    assert out.read_bytes() == before_csv  # l'ultimo CSV valido resta intatto

    # import bloccato per dato invalido (pfc=0) sul stesso out/meta
    row = make_row("MARCO R.", "Milano", "A", 60.0)
    row[FIELD_IDX["pfc"]] = 0
    write_workbook(tmp_path / "bad2.xlsx", [row])
    r = il.import_listone(str(tmp_path / "bad2.xlsx"), str(out), str(meta))
    assert r["ok"] is False
    assert out.read_bytes() == before_csv
    assert meta.read_bytes() == before_meta


# ---------------------------------------------------------------- range sporco
def test_range_sporco_warning_senza_inventare(tmp_path):
    row = make_row("MARCO R.", "Milano", "A", 60.0)
    row[FIELD_IDX["pfcRange"]] = "40..70"  # separatore non riconosciuto
    write_workbook(tmp_path / "l.xlsx", [row])
    r = il.import_listone(
        str(tmp_path / "l.xlsx"), str(tmp_path / "o.csv"), str(tmp_path / "m.json")
    )
    assert r["ok"] is True  # warning non bloccante
    assert any("range PFC non parsabile" in w for w in r["warnings"])
    assert r["players"][0]["pfc_lo"] is None
    assert r["players"][0]["unc_pfc"] is None  # mai dati inventati
    assert r["players"][0]["pfc_range"] == "40..70"  # stringa preservata


# ------------------------------------------------- load_players backward-compat
LEGACY_FIELDS = [
    "nome",
    "squadra",
    "ruolo",
    "pfc",
    "pma",
    "pfc_range",
    "pma_range",
    "dpfcpma",
    "slot",
    "tit",
    "expfm",
    "fascia",
    "status",
    "pen_prob",
    "fk_prob",
    "tix",
    "fix",
    "fix_contrib",
]


def _write_legacy(path, row_dicts):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEGACY_FIELDS)
        w.writeheader()
        w.writerows(row_dicts)


def test_load_players_csv_legacy_deriva_pid_e_range(tmp_path):
    legacy = tmp_path / "legacy.csv"
    _write_legacy(
        legacy,
        [
            {
                "nome": "MARCO R.",
                "squadra": "Milano",
                "ruolo": "A",
                "pfc": "60",
                "pma": "58",
                "pfc_range": "54-66",
                "pma_range": "52-64",
                "dpfcpma": "2",
                "slot": "2",
                "tit": "70",
                "expfm": "6.5",
                "fascia": "Top",
                "status": "T",
                "pen_prob": "10",
                "fk_prob": "0",
                "tix": "80",
                "fix": "70",
                "fix_contrib": "0.35",
            }
        ],
    )
    players = load_players(str(legacy))
    assert len(players) == 1
    p = players[0]
    assert p["pid"] == compute_pid("MARCO R.", "A")  # derivato con lo stesso algoritmo
    assert p["pfc_lo"] == 54.0 and p["pfc_hi"] == 66.0  # parse dai range stringa
    assert p["pma_lo"] == 52.0 and p["pma_hi"] == 64.0
    assert p["unc_pfc"] == 6.0
    assert p["slot"] == 2


def test_load_players_pfc_malformato_errore_contestualizzato(tmp_path):
    legacy = tmp_path / "bad.csv"
    _write_legacy(
        legacy,
        [{"nome": "MARCO R.", "squadra": "Milano", "ruolo": "A", "pfc": "abc"}],
    )
    with pytest.raises(ValueError, match="pfc"):
        load_players(str(legacy))


def test_load_players_numero_presente_malformato_errore(tmp_path):
    legacy = tmp_path / "bad2.csv"
    _write_legacy(
        legacy,
        [
            {
                "nome": "MARCO R.",
                "squadra": "Milano",
                "ruolo": "A",
                "pfc": "60",
                "tit": "settanta",  # colonna presente ma malformata: niente default muto
            }
        ],
    )
    with pytest.raises(ValueError, match="tit"):
        load_players(str(legacy))


# --------------------------------------------------------- lookup motore (pid)
def test_lookup_pid_e_nome_sul_motore():
    players = [
        make_player("MARCO R.", ruolo="A", pfc=50.0),
        make_player("LUCA D.", ruolo="D", pfc=40.0),
        make_player("PAOLO", ruolo="C", pfc=30.0),
    ]
    a = Auction([dict(q) for q in players], teams=2, budget=100)
    pid_marco = compute_pid("MARCO R.", "A")

    # lookup per nome legacy
    assert a.find("MARCO R.")["pid"] == pid_marco
    assert a.find("MARCO")["pid"] == pid_marco  # substring per nome
    assert a.resolve("MARCO R.") == pid_marco
    # lookup per pid
    assert a.find(pid_marco)["nome"] == "MARCO R."
    assert a.resolve(pid_marco) == pid_marco
    # assente
    assert a.find("INESISTENTE") is None
    assert a.resolve("INESISTENTE") is None
    # ogni player ha sempre il pid e i pool/sold usano pid
    assert set(a.players) == {
        compute_pid("MARCO R.", "A"),
        compute_pid("LUCA D.", "D"),
        compute_pid("PAOLO", "C"),
    }
    assert a.state["pool"] == set(a.players)


def test_lookup_omonimi_ruoli_diversi_ambigui():
    players = [
        make_player("ROSSI", ruolo="D", pfc=30.0),
        make_player("ROSSI", ruolo="C", pfc=25.0),
    ]
    a = Auction([dict(q) for q in players], teams=2, budget=100)
    with pytest.raises(AmbiguousName):
        a.find("ROSSI")
    with pytest.raises(AmbiguousName):
        a.resolve("ROSSI")
    # ma il pid esatto disambigua
    pid_d = compute_pid("ROSSI", "D")
    assert a.find(pid_d)["ruolo"] == "D"


def test_engine_collisione_stesso_nome_ruolo_blocca():
    players = [
        make_player("ROSSI", ruolo="D", squadra="Roma", pfc=30.0),
        make_player("ROSSI", ruolo="D", squadra="Torino", pfc=25.0),
    ]
    with pytest.raises(ConfigError, match="collisione"):
        Auction([dict(q) for q in players], teams=2, budget=100)


# -------------------------------------------------- contratti API/export (pid)
def test_api_contratto_pid_e_export():
    pool = [make_player(f"A{i:02d}", pfc=float(30 + i)) for i in range(1, 9)]
    wa.PLAYERS = [dict(q) for q in pool]
    wa.engine = wa.TrendAuction([dict(q) for q in pool], teams=3, budget=100, io="IO")

    # eval per nome legacy e per pid: key == pid, pid presente nel payload
    resp = wa.api_eval(key="A01")
    assert resp["key"] == resp["pid"] == compute_pid("A01", "A")
    assert wa.api_eval(key=resp["pid"])["nome"] == "A01"
    # ricerca per nome ancora funzionante
    res = wa.api_players(q="A0")
    assert {r["nome"] for r in res["results"]} == {f"A{i:02d}" for i in range(1, 9)}
    assert all(r["key"] == r["pid"] for r in res["results"])

    # vendita via nome legacy: l'evento registra il pid
    assert wa.api_sold(wa.SoldBody(key="A02", price=20, team="IO"))["ok"]
    ev = wa.engine.events[0]
    assert ev["kind"] == "sold"
    assert ev["pid"] == compute_pid("A02", "A")

    # export con la colonna Pid (rose e svincolati)
    rose = wa.export_rose()
    rose_text = bytes(rose.body).decode("utf-8")
    assert "Pid" in rose_text and compute_pid("A02", "A") in rose_text
    sv = wa.export_svincolati()
    sv_text = bytes(sv.body).decode("utf-8")
    assert "Pid" in sv_text and compute_pid("A01", "A") in sv_text


# ------------------------------------------------------------------- CLI
def test_cli_exit_code_2_su_file_invalido(tmp_path):
    header = [h for h in HEADER if h != "pfcRange"]
    write_workbook(
        tmp_path / "bad.xlsx",
        [make_row("MARCO R.", "Milano", "A", 60.0)],
        header=header,
    )
    out = tmp_path / "out.csv"
    rep = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_listone.py"),
            "--file",
            str(tmp_path / "bad.xlsx"),
            "--out",
            str(out),
            "--report",
            str(rep),
            "--meta",
            str(tmp_path / "meta.json"),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    assert proc.returncode == 2  # errore bloccante
    assert "BLOCCATO" in proc.stdout
    assert json.loads(rep.read_text(encoding="utf-8"))["ok"] is False
    assert not out.exists()


def test_cli_exit_code_0_su_file_valido(tmp_path):
    write_workbook(tmp_path / "ok.xlsx", [make_row("MARCO R.", "Milano", "A", 60.0)])
    out = tmp_path / "out.csv"
    rep = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_listone.py"),
            "--file",
            str(tmp_path / "ok.xlsx"),
            "--out",
            str(out),
            "--report",
            str(rep),
            "--meta",
            str(tmp_path / "meta.json"),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(rep.read_text(encoding="utf-8"))["ok"] is True
    assert out.exists()
    rd = list(csv.DictReader(out.open("r", encoding="utf-8-sig", newline="")))
    assert len(rd) == 1 and rd[0]["pid"] == compute_pid("MARCO R.", "A")
