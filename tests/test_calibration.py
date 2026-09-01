"""Test della calibrazione adattiva del mercato dalle vendite reali (WP8).

Coprono il modulo puro ``scripts/calibration.py`` (calibration_from_events /
estimate_player / CalibrationConfig) e il wiring advisory in web_auction
(``GET /api/calibration``, blocco ``calibration`` in /api/eval, flag
``use_calibration_in_price`` esposto ma NON applicato) + la validazione della
config nel dominio (league_config).

Contratto verificato:
- vendite ATTIVE (sold non revocato) -> cella (ruolo, fascia prezzo, fase);
- centro robusto = mediana ponderata del ``premium_pfc``, spread = MAD
  ponderata; pesi recency esponenziali half-life; un outlier non sposta la
  mediana;
- shrinkage gerarchico cella -> ruolo/fase -> ruolo -> globale con
  ``w = n_eff/(n_eff+k)``, fattori bounded in [0.5, 2.0], CI bounded che
  contiene sempre il fattore, confidence low/medium/high;
- zero dati -> fattore neutro 1.0, CI neutra, ``nodata``;
- estimate_player: ``applied=False`` + ``reason="advisory_gate_off"``, mai
  dentro ``suggested``/``maxbid``/``market.expected``;
- fallback gerarchico cella -> ruolo/fase -> ruolo -> globale -> neutro;
- fase deterministica dal progresso dell'asta (start/mid/end, league|role);
- API: /api/calibration espone celle con n/CI e il flag advisory;
- replay undo/correct riflette SOLO gli eventi attivi (engine.events).
"""

import json

import calibration  # pyright: ignore[reportMissingImports]
import league_config  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from auction_store import AuctionStore  # pyright: ignore[reportMissingImports]
from conftest import make_player  # pyright: ignore[reportMissingImports]

ROLE_ORDER = ("P", "D", "C", "A")
BANDS = ("<10", "10-25", "25-50", "50-100", "100-200", "200+")

DEFAULT_CFG = {
    "teams": 3,
    "budget": 100,
    "slots": {"P": 3, "D": 3, "C": 3, "A": 6},
    "formation": {"P": 1, "D": 1, "C": 1, "A": 4},
    "io": "IO",
    "team_names": ["IO", "T1", "T2"],
}

DEFAULT_SLOTS = {"P": 3, "D": 3, "C": 3, "A": 6}
DEFAULT_FORMATION = {"P": 1, "D": 1, "C": 1, "A": 4}


def make_pool(p=0, d=0, c=0, a=8):
    """Pool di giocatori con nomi per ruolo (P01.., D01.., C01.., A01..)."""
    players = []
    for role, n in (("P", p), ("D", d), ("C", c), ("A", a)):
        players += [make_player(f"{role}{i:02d}", ruolo=role) for i in range(1, n + 1)]
    return players


# ---------------------------------------------------------------- eventi
def sold_event(i, ruolo="A", base=50.0, price=None, premium=None):
    """Evento ``sold`` nello schema di ``TrendAuction.events`` (kind, i, ...)."""
    price = int(base) if price is None else price
    e = {
        "i": i,
        "kind": "sold",
        "pid": f"{ruolo}{i:02d}",
        "nome": f"{ruolo}{i:02d}",
        "ruolo": ruolo,
        "infl": 1.0,
        "price": price,
        "team": "IO",
        "base": base,
    }
    if premium is not None:
        e["premium_pfc"] = premium
    return e


def unsold_event(i, ruolo="A"):
    return {"i": i, "kind": "unsold", "pid": f"{ruolo}{i:02d}", "nome": f"{ruolo}{i:02d}", "ruolo": ruolo}


def store_sold_event(seq, ruolo="A", base=50.0, price=None):
    """Evento ``sold`` nello schema del log store (type + payload)."""
    price = int(base) if price is None else price
    return {
        "seq": seq,
        "type": "sold",
        "payload": {
            "pid": f"{ruolo}{seq:02d}",
            "nome": f"{ruolo}{seq:02d}",
            "ruolo": ruolo,
            "price": price,
            "team": "IO",
            "base": base,
        },
    }


def premium_events(n, premium=1.0, ruolo="A", base=50.0, start=0):
    """``n`` vendite con lo stesso premium (indici consecutivi da ``start``)."""
    return [sold_event(i, ruolo=ruolo, base=base, price=round(base * premium)) for i in range(start, start + n)]


def stats_of(rep, key="global"):
    """Statistiche di un livello del report (dict JSON di ``to_dict()``)."""
    return rep.to_dict()[key]


# ========================================================================
# modulo puro: baseline, robustezza, shrinkage, CI
# ========================================================================
def test_nodata_report_fattore_neutro_e_ci_neutra():
    rep = calibration.calibration_from_events([], DEFAULT_CFG)
    assert rep.nodata is True and rep.n_sales == 0
    g = stats_of(rep)
    assert g["factor"] == 1.0 and g["n"] == 0 and g["nodata"] is True
    assert g["ci_lo"] == 0.5 and g["ci_hi"] == 1.5  # factor ± 0.5 (default), bounded
    # ogni ruolo/fase/cella esiste ma e' nodata con fattore neutro
    assert rep.roles["A"].factor == 1.0 and rep.roles["A"].nodata
    assert rep.role_phases[("A", "start")].nodata
    # la CI neutra contiene sempre il fattore
    assert g["ci_lo"] <= g["factor"] <= g["ci_hi"]


def test_mediana_ponderata_e_mad():
    evs = [sold_event(i, base=50.0, premium=p) for i, p in enumerate([1.0, 1.5, 1.1])]
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    g = stats_of(rep)
    # mediana di [1.0, 1.1, 1.5] = 1.1 (pesi quasi uniformi, half_life 25)
    assert g["raw_center"] == pytest.approx(1.1)
    # MAD dei valori attorno a 1.1: |1.0-1.1|=0.1, |1.1-1.1|=0, |1.5-1.1|=0.4 -> 0.1
    assert g["mad"] == pytest.approx(0.1)


def test_outlier_non_sposta_la_mediana():
    evs = [sold_event(i, base=50.0, premium=p) for i, p in enumerate([1.0, 1.05, 1.1, 1.2, 3.0])]
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    g = stats_of(rep)
    # mediana = 1.1 (il 3.0 resta un singolo valore sopra la meta' pesata)
    assert g["raw_center"] == pytest.approx(1.1)
    # il fattore (shrunk, bounded) resta lontano dall'outlier
    assert g["factor"] < 1.5
    assert g["factor"] >= 0.5


def test_n_piccolo_shrink_verso_prior():
    cfg = dict(DEFAULT_CFG, calibration_k=10.0)
    # una sola vendita a premium 2.2 nel ruolo D: n_eff ~ 1 -> w ~ 1/11
    rep = calibration.calibration_from_events([sold_event(0, ruolo="D", premium=2.2)], cfg)
    rl = rep.roles["D"]
    # prior = fattore globale (solo questa vendita -> center globale 2.2, n_eff 1)
    assert rl.prior == pytest.approx(rep.global_stats.factor)
    w = rl.n_eff / (rl.n_eff + cfg["calibration_k"])
    expected = w * 2.2 + (1 - w) * rep.global_stats.factor
    assert rl.factor == pytest.approx(expected)
    # con n cosi' piccolo il fattore resta piu' vicino al prior che al dato grezzo
    assert abs(rl.factor - rep.global_stats.factor) < abs(rl.center - rep.global_stats.factor)
    # n alto -> il dato domina il prior
    many = premium_events(200, premium=2.2, ruolo="D")
    rep2 = calibration.calibration_from_events(many, cfg)
    assert rep2.roles["D"].factor == pytest.approx(
        min(2.0, 2.2), abs=0.05
    )  # bounded a 2.0


def test_shrinkage_gerarchico_prior_del_genitore():
    # vendite solo nel ruolo A, fascia 50-100, fase start; prior di ogni livello
    # == fattore gia' shrunk del livello genitore (bottom-up).
    evs = premium_events(40, premium=1.2, ruolo="A", base=50.0)
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    cell = rep.cells[("A", "50-100", "start")]
    assert cell.prior == pytest.approx(rep.role_phases[("A", "start")].factor)
    assert rep.role_phases[("A", "start")].prior == pytest.approx(rep.roles["A"].factor)
    assert rep.roles["A"].prior == pytest.approx(rep.global_stats.factor)
    # i livelli senza dati (role D / fase end) ereditano il prior del genitore
    assert rep.roles["D"].factor == pytest.approx(rep.global_stats.factor, abs=1e-3)
    assert rep.roles["D"].nodata


def test_ci_piu_stretta_con_piu_n():
    # stessa distribuzione (mediana e MAD identici) ma n doppio: la CI si stringe
    dist = [1.0, 1.1, 1.2, 1.3]
    evs4 = [sold_event(i, premium=dist[i]) for i in range(4)]
    evs8 = [sold_event(i, premium=dist[i % 4]) for i in range(8)]
    rep4 = calibration.calibration_from_events(evs4, DEFAULT_CFG)
    rep8 = calibration.calibration_from_events(evs8, DEFAULT_CFG)
    g4, g8 = stats_of(rep4), stats_of(rep8)
    assert g4["raw_center"] == pytest.approx(g8["raw_center"])
    assert g4["mad"] == pytest.approx(g8["mad"])
    assert g8["n"] == 2 * g4["n"]
    assert g8["ci_half_width"] < g4["ci_half_width"]
    assert g8["n_eff"] > g4["n_eff"]
    # 40 vendite -> confidence high; 5 -> low (n_eff >= 30 -> high, >= 10 -> medium)
    repH = calibration.calibration_from_events(premium_events(40, premium=1.1), DEFAULT_CFG)
    repL = calibration.calibration_from_events(premium_events(5, premium=1.1), DEFAULT_CFG)
    assert stats_of(repH)["confidence"] == "high"
    assert stats_of(repL)["confidence"] == "low"


def test_ci_contiene_sempre_il_fattore_e_bounded():
    # estremi: vendite a premium 9x e 0.2x -> fattori ai bound; CI dentro i bound
    for evs in (premium_events(60, premium=9.0), premium_events(60, premium=0.2)):
        rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
        d = rep.to_dict()
        for key, stats in d["roles"].items():
            assert 0.5 <= stats["factor"] <= 2.0, key
            assert 0.5 <= stats["ci_lo"] <= stats["factor"] <= stats["ci_hi"] <= 2.0
        for key, stats in d["cells"].items():
            assert 0.5 <= stats["factor"] <= 2.0, key
            assert stats["ci_lo"] <= stats["factor"] <= stats["ci_hi"]
        g = d["global"]
        assert 0.5 <= g["factor"] <= 2.0
        assert g["ci_lo"] <= g["factor"] <= g["ci_hi"] <= 2.0


def test_tutti_i_numeri_finiti_su_input_strano():
    # premium/basi che produrrebbero divisioni o NaN restano fuori; niente crash
    evs = [
        {"kind": "sold", "i": 0, "ruolo": "A", "base": 0.0, "price": 5},
        {"kind": "sold", "i": 1, "ruolo": "A", "base": 50.0, "price": -3},
        {"kind": "sold", "i": 2, "ruolo": "A", "base": 50.0, "premium_pfc": None},
        {"kind": "sold", "i": 3, "ruolo": "Z", "base": 50.0, "price": 60},
        {"kind": "unsold", "i": 4, "ruolo": "A", "base": 50.0},
        {"kind": "sold", "i": 5, "ruolo": "A", "base": 50.0, "price": 60},
    ]
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    # solo l'ultima vendita e' normalizzabile (premium = 60/50 = 1.2)
    assert rep.n_sales == 1
    d = rep.to_dict()
    assert d["global"]["n"] == 1 and d["global"]["factor"] >= 0.5
    for stats in d["roles"].values():
        for v in ("factor", "ci_lo", "ci_hi", "prior"):
            assert isinstance(stats[v], (int, float)) and stats[v] == stats[v]


def test_deterministico_byte_identico():
    evs = premium_events(30, premium=1.3, ruolo="C", base=40.0)
    rep1 = calibration.calibration_from_events([dict(e) for e in evs], DEFAULT_CFG)
    rep2 = calibration.calibration_from_events([dict(e) for e in evs], DEFAULT_CFG)
    assert json.dumps(rep1.to_dict(), sort_keys=True, separators=(",", ":")) == json.dumps(
        rep2.to_dict(), sort_keys=True, separators=(",", ":")
    )


# ========================================================================
# segmentazione: bande, fasi, recency
# ========================================================================
def test_bande_prezzo_fisse():
    cfg = CalibratedCfg()
    assert calibration.band_for_base(5.0, cfg) == "<10"
    assert calibration.band_for_base(10.0, cfg) == "10-25"
    assert calibration.band_for_base(25.0, cfg) == "25-50"
    assert calibration.band_for_base(50.0, cfg) == "50-100"
    assert calibration.band_for_base(100.0, cfg) == "100-200"
    assert calibration.band_for_base(200.0, cfg) == "200+"
    assert calibration.band_for_base(0.5, cfg) == "<10"  # sotto la prima banda
    assert calibration.band_for_base(1000.0, cfg) == "200+"  # sopra l'ultima


def CalibratedCfg():
    return calibration.CalibrationConfig()


def test_segmentazione_celle_ruolo_banda_fase():
    # vendite in ruoli/fasce/base diversi -> celle separate con identita' corretta
    evs = [
        sold_event(0, ruolo="A", base=50.0, premium=1.2),
        sold_event(1, ruolo="A", base=120.0, premium=1.5),
        sold_event(2, ruolo="D", base=30.0, premium=1.0),
        sold_event(3, ruolo="P", base=5.0, premium=1.3),
    ]
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    assert set(rep.cells) == {
        ("A", "50-100", "start"),
        ("A", "100-200", "start"),
        ("D", "25-50", "start"),
        ("P", "<10", "start"),
    }
    for key, cell in rep.cells.items():
        assert (cell.role, cell.band, cell.phase) == key
        assert cell.n == 1 and not cell.nodata


def test_fase_league_deterministica_da_progresso():
    # slot totali = 3 x (3+3+3+6) = 45; frazioni 1/3 e 2/3 -> 15 e 30 vendite
    evs15 = premium_events(15, premium=1.0)
    evs16 = premium_events(16, premium=1.0)
    evs31 = premium_events(31, premium=1.0)
    assert calibration.calibration_from_events(evs15, DEFAULT_CFG).phase_global == "start"
    assert calibration.calibration_from_events(evs16, DEFAULT_CFG).phase_global == "mid"
    # 16/45 = 0.356 (mid), 30/45 = 0.667 > 2/3? no: 0.6667 -> end al > 2/3
    assert calibration.calibration_from_events(evs31, DEFAULT_CFG).phase_global == "end"
    # in modalita' league ogni ruolo ha la fase globale
    rep = calibration.calibration_from_events(evs16, DEFAULT_CFG)
    assert set(rep.phase_roles.values()) == {"mid"}


def test_fase_role_su_progresso_del_ruolo():
    cfg = dict(DEFAULT_CFG, calibration_phase_mode="role")
    # ruolo A: slot = 3 x 6 = 18; 6 vendite -> 6/18 = 1/3 esatto -> start (< 1/3? 6/18=0.3333)
    evs = premium_events(6, premium=1.0, ruolo="A")
    rep = calibration.calibration_from_events(evs, cfg)
    assert rep.phase_roles["A"] == "start"
    # 7 vendite -> 7/18 = 0.389 -> mid
    rep2 = calibration.calibration_from_events(premium_events(7, premium=1.0, ruolo="A"), cfg)
    assert rep2.phase_roles["A"] == "mid"
    # fine: 13/18 = 0.72 (> 2/3) -> end
    rep3 = calibration.calibration_from_events(premium_events(13, premium=1.0, ruolo="A"), cfg)
    assert rep3.phase_roles["A"] == "end"
    # ruoli senza vendite -> stessa fase del globale (fallback del denominatore)
    assert rep.phase_roles["D"] == rep.phase_global


def test_recency_esponenziale_half_life():
    # eventi vecchi a premium 1.0 + uno recente a 2.0: con half_life piccolo il
    # peso dell'evento recente domina la mediana ponderata
    evs = [
        sold_event(0, premium=1.0),
        sold_event(1, premium=1.0),
        sold_event(10, premium=2.0),
    ]
    rep_short = calibration.calibration_from_events(
        [dict(e) for e in evs], dict(DEFAULT_CFG, calibration_half_life=1.0)
    )
    rep_uniform = calibration.calibration_from_events(
        [dict(e) for e in evs], dict(DEFAULT_CFG, calibration_half_life=1e9)
    )
    # mediana ponderata: quasi-uniforme -> 1.0; recency forte -> 2.0
    assert stats_of(rep_uniform)["raw_center"] == pytest.approx(1.0)
    assert stats_of(rep_short)["raw_center"] == pytest.approx(2.0)
    # l'evento piu' recente ha sempre peso 1 (w_i = 2^((i - i_max)/half_life))
    assert stats_of(rep_short)["n"] == 3


def test_n_eff_di_kish_uguale_n_con_pesi_uniformi():
    evs = premium_events(10, premium=1.0)
    rep = calibration.calibration_from_events(evs, dict(DEFAULT_CFG, calibration_half_life=1e9))
    n_eff = rep.global_stats.n_eff
    assert n_eff == pytest.approx(10.0, abs=0.05)
    assert rep.global_stats.n == 10


# ========================================================================
# estimate_player: fallback, nodata, contratto advisory
# ========================================================================
def test_estimate_player_fallback_gerarchico():
    # dati solo in ruolo A, fascia 50-100, fase start (10/45 < 1/3 -> start)
    evs = premium_events(10, premium=1.2, ruolo="A", base=50.0)
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    # nella cella -> source cell, ESATTAMENTE il fattore della cella (nessun
    # ri-shrinkage: il valore serializzato di estimate_player coincide con
    # quello serializzato della cella)
    est = calibration.estimate_player(rep, {"ruolo": "A", "base": 60.0, "pid": "x", "nome": "x"})
    assert est["source"] == "cell"
    assert est["factor"] == rep.cells[("A", "50-100", "start")].to_dict()["factor"]
    assert est["cell"] == {"role": "A", "band": "50-100", "phase": "start"}
    # fascia senza dati (ma stesso ruolo/fase) -> ruolo/fase
    est2 = calibration.estimate_player(rep, {"ruolo": "A", "base": 20.0, "pid": "x", "nome": "x"})
    assert est2["source"] == "role_phase"
    assert est2["cell"]["band"] == "10-25"
    # ruolo senza dati -> globale
    est3 = calibration.estimate_player(rep, {"ruolo": "D", "base": 60.0, "pid": "x", "nome": "x"})
    assert est3["source"] == "global"
    assert est3["factor"] == rep.to_dict()["global"]["factor"]


def test_estimate_player_nodata_e_senza_base():
    empty = calibration.calibration_from_events([], DEFAULT_CFG)
    est = calibration.estimate_player(empty, {"ruolo": "A", "base": 50.0, "pid": "x", "nome": "x"})
    assert est["source"] == "nodata" and est["nodata"] is True
    assert est["factor"] == 1.0
    assert est["ci_lo"] == 0.5 and est["ci_hi"] == 1.5
    # giocatore senza ``base``: nessuna cella -> fallback al livello superiore
    evs = premium_events(40, premium=1.2, ruolo="A", base=50.0)
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    est2 = calibration.estimate_player(rep, {"ruolo": "A", "pid": "x", "nome": "x"})
    assert est2["source"] == "role_phase" and est2["cell"]["band"] is None


def test_estimate_player_contratto_advisory():
    evs = premium_events(40, premium=1.5, ruolo="A", base=50.0)
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    est = calibration.estimate_player(rep, {"ruolo": "A", "base": 50.0, "pid": "x", "nome": "x"}, expected=100)
    assert set(est) == {
        "factor", "expected_if_applied", "range", "applied", "reason", "cell",
        "source", "nodata", "n", "n_eff", "mad", "raw_center", "prior",
        "ci_lo", "ci_hi", "ci_half_width", "confidence",
    }
    assert est["applied"] is False and est["reason"] == "advisory_gate_off"
    # expected_if_applied = round(expected x factor), range da ci_lo/ci_hi
    expected = round(100 * est["factor"])
    assert est["expected_if_applied"] == expected
    assert est["range"]["lo"] == max(1, min(round(100 * est["ci_lo"]), expected))
    assert est["range"]["hi"] == max(round(100 * est["ci_hi"]), expected)
    assert est["range"]["lo"] <= expected <= est["range"]["hi"]
    # senza expected -> base del giocatore
    est2 = calibration.estimate_player(rep, {"ruolo": "A", "base": 80.0, "pid": "x", "nome": "x"})
    assert est2["expected_if_applied"] == round(80 * est2["factor"])


def test_estimate_player_current_phase_esplicito():
    evs = (
        premium_events(20, premium=1.4, ruolo="A", base=50.0, start=0)
        + premium_events(30, premium=1.0, ruolo="A", base=50.0, start=20)
    )
    rep = calibration.calibration_from_events(evs, DEFAULT_CFG)
    est_start = calibration.estimate_player(
        rep, {"ruolo": "A", "base": 50.0, "pid": "x", "nome": "x"}, current_phase="start"
    )
    est_end = calibration.estimate_player(
        rep, {"ruolo": "A", "base": 50.0, "pid": "x", "nome": "x"}, current_phase="end"
    )
    # fase start premium alto, fase end premium basso: fattori distinti
    assert est_start["factor"] > est_end["factor"]
    assert est_start["cell"]["phase"] == "start"
    assert est_end["cell"]["phase"] == "end"


# ========================================================================
# config: CalibrationConfig / parse_calibration_params / league_config
# ========================================================================
def test_calibration_config_to_dict_e_validate():
    cfg = calibration.CalibrationConfig()
    assert cfg.validate() == []
    d = cfg.to_dict()
    assert d["price_bands"][-1][1] is None  # +inf -> None in JSON
    assert d["factor_min"] == 0.5 and d["factor_max"] == 2.0
    assert d["phase_mode"] == "league"
    # bande custom crescono; k<=0 / half_life<=0 / bounds invertiti -> errori
    bad = calibration.CalibrationConfig(
        price_bands=((10.0, 5.0),), k=-1, half_life=0, factor_min=3, factor_max=1, ci_max_half_width=0
    )
    errs = bad.validate()
    assert any("prezzo" not in e and "bande" in e for e in errs) or any("banda" in e for e in errs)
    assert any("calibration_k" in e for e in errs)
    assert any("half_life" in e for e in errs)
    assert any("factor_min" in e or "factor_max" in e for e in errs)
    assert any("ci_max_half_width" in e for e in errs)


def test_parse_calibration_params_default_e_override():
    cfg, errs = calibration.parse_calibration_params(None)
    assert errs == [] and cfg == calibration.CalibrationConfig()
    cfg2, errs2 = calibration.parse_calibration_params(
        {
            "calibration_k": 5,
            "calibration_half_life": 10,
            "calibration_phase_mode": "role",
            "calibration_price_bands": [[0, 20], [20, None]],
            "calibration_factor_min": 0.4,
            "calibration_factor_max": 2.5,
        }
    )
    assert errs2 == []
    assert cfg2.k == 5 and cfg2.half_life == 10 and cfg2.phase_mode == "role"
    assert cfg2.price_bands == ((0.0, 20.0), (20.0, float("inf")))
    assert cfg2.factor_min == 0.4 and cfg2.factor_max == 2.5
    # parametri invalidi -> errori (default preservati)
    cfg3, errs3 = calibration.parse_calibration_params(
        {"calibration_k": "x", "calibration_phase_mode": "boh", "calibration_price_bands": [[10, 5]]}
    )
    assert any("calibration_k" in e for e in errs3)
    assert any("phase_mode" in e for e in errs3)
    assert any("banda" in e or "calibration_price_bands" in e for e in errs3)
    assert cfg3.k == calibration.DEFAULT_K  # default preservato


def test_league_config_valida_flag_e_parametri_calibration():
    # flag non booleano -> errore; assente -> ok (default False)
    assert "use_calibration_in_price" in " ".join(
        league_config.validate(dict(DEFAULT_CFG, use_calibration_in_price="si"))
    )
    assert league_config.validate(dict(DEFAULT_CFG, use_calibration_in_price=True)) == []
    assert league_config.validate(DEFAULT_CFG) == []
    # calibration_k invalido -> errore dalla fonte unica (calibration)
    errs = league_config.validate(dict(DEFAULT_CFG, calibration_k=0))
    assert any("calibration_k" in e for e in errs)
    # bande sovrapposte -> errore
    errs2 = league_config.validate(
        dict(DEFAULT_CFG, calibration_price_bands=[[0, 30], [20, 50]])
    )
    assert any("bande" in e for e in errs2)
    # parametri validi passano il validate pubblico
    assert league_config.validate(
        dict(
            DEFAULT_CFG,
            calibration_k=8,
            calibration_phase_mode="role",
            calibration_price_bands=[[0, 20], [20, None]],
        )
    ) == []


def test_use_calibration_flag_esposto_ma_mai_applicato_al_prezzo():
    # il flag non cambia NIENTE nel prezzo: e' solo esposto (WP8 advisory)
    cfg_on = dict(DEFAULT_CFG, use_calibration_in_price=True)
    cfg_off = dict(DEFAULT_CFG, use_calibration_in_price=False)
    evs = premium_events(60, premium=3.0)  # vendite anomale
    rep_on = calibration.calibration_from_events(evs, cfg_on)
    rep_off = calibration.calibration_from_events(evs, cfg_off)
    assert rep_on.global_stats.factor == rep_off.global_stats.factor
    est_on = calibration.estimate_player(rep_on, {"ruolo": "A", "base": 50.0})
    assert est_on["applied"] is False and est_on["reason"] == "advisory_gate_off"


# ========================================================================
# API: /api/calibration e advisory in /api/eval (prezzi INVARIATI)
# ========================================================================
def set_engine(pool, **overrides):
    """Motore web isolato per test (stesso pattern di test_web_auction)."""
    wa.PLAYERS = pool
    overrides.setdefault("slots", dict(DEFAULT_SLOTS))
    overrides.setdefault(
        "formation",
        {r: min(DEFAULT_FORMATION[r], overrides["slots"][r]) for r in DEFAULT_FORMATION},
    )
    wa.engine = wa.TrendAuction([dict(q) for q in pool], **overrides)
    return wa.engine


@pytest.fixture
def engine():
    # pool COPRENTE (serve per POST /api/config: feasibility richiede abbastanza
    # giocatori per ruolo rispetto a teams x slots normalizzati)
    return set_engine(make_pool(p=9, d=24, c=24, a=18), teams=3, budget=100, io="IO")


def test_api_calibration_espone_celle_n_ci_e_flag(engine):
    wa.api_sold(wa.SoldBody(key="A01", price=60, team="IO"))  # base 50 -> premium 1.2
    wa.api_sold(wa.SoldBody(key="A02", price=65, team="T1"))  # squadra diversa: nel budget
    data = wa.api_calibration()
    assert isinstance(data, dict)
    assert data["use_calibration_in_price"] is False
    assert data["advisory"] is True and data["reason"] == "advisory_gate_off"
    assert data["n_sales"] == 2 and data["nodata"] is False
    assert data["phase"]["global"] in ("start", "mid", "end")
    # base 50 appartiene alla banda 50-100 ([50, 100))
    cell = data["cells"]["A|50-100|start"]
    assert cell["n"] == 2 and cell["confidence"] in ("low", "medium", "high")
    assert cell["ci_lo"] <= cell["factor"] <= cell["ci_hi"]
    assert 0.5 <= cell["factor"] <= 2.0
    # il report JSON e' serializzabile (niente float non finiti)
    json.dumps(data)


def test_api_calibration_nodata_senza_vendite(engine):
    data = wa.api_calibration()
    assert data["nodata"] is True and data["n_sales"] == 0
    assert data["global"]["factor"] == 1.0


def test_api_eval_advisory_e_prezzi_invariati(engine):
    p = engine.find("A01")
    before = wa.player_payload(engine, p, "IO")
    assert "calibration" in before
    assert before["calibration"]["applied"] is False
    assert before["calibration"]["reason"] == "advisory_gate_off"
    # vendite REALI (scenari validi: squadre diverse, prezzi nel budget) creano
    # dati di calibrazione; inflazione/scarsita' NATURALMENTE cambiano i prezzi
    # dell'asta, quindi i prezzi prima/dopo NON si confrontano. L'invariante e'
    # altro: sullo STESSO stato, la calibrazione NON modifica i prezzi, che
    # coincidono con quelli del motore puro (engine.evaluate).
    wa.api_sold(wa.SoldBody(key="A02", price=90, team="T1"))  # premium 1.8x
    wa.api_sold(wa.SoldBody(key="A03", price=25, team="T2"))  # premium 0.5x
    after = wa.api_eval(key="A01", team="IO")
    direct = engine.evaluate(p, "IO")
    # ora la calibrazione HA dati (fattore != 1) ma resta advisory
    assert after["calibration"]["factor"] != 1.0
    assert after["calibration"]["applied"] is False
    assert after["calibration"]["source"] in ("cell", "role_phase", "role", "global", "nodata")
    # sullo STESSO stato: i prezzi non sono toccati dalla calibrazione
    assert after["suggested"] == direct["suggested"]
    assert after["maxbid"] == direct["maxbid"]
    assert after["market"]["expected"] == direct["market"]["expected"]


def test_api_urls_conflagration_flag_config(engine):
    # POST /api/config con flag True -> esposto in /api/config e /api/calibration
    resp = wa.api_config_post(
        wa.ConfigBody(
            teams=2, budget=200, names=["IO", "T1"], io="IO",
            use_calibration_in_price=True,
        )
    )
    assert isinstance(resp, dict) and resp.get("ok") is True
    assert wa.api_config()["use_calibration_in_price"] is True
    assert wa.api_calibration()["use_calibration_in_price"] is True
    # il prezzo del suggerito NON cambia col flag (advisory-only)
    before = wa.api_eval(key="A01", team="IO")["suggested"]
    resp2 = wa.api_config_post(
        wa.ConfigBody(
            teams=2, budget=200, names=["IO", "T1"], io="IO",
            use_calibration_in_price=False,
        )
    )
    assert isinstance(resp2, dict) and resp2.get("ok") is True
    assert wa.api_config()["use_calibration_in_price"] is False
    assert wa.api_eval(key="A01", team="IO")["suggested"] == before


# ========================================================================
# replay: undo/correct usano gli eventi ATTIVI
# ========================================================================
POOL_COVERING = make_pool(p=9, d=18, c=18, a=18)


@pytest.fixture
def persisted(tmp_path):
    """Web engine con store attivo su DB temporaneo (pattern test_persistence_api)."""
    wa.PLAYERS = list(POOL_COVERING)
    st = AuctionStore(tmp_path / "asta.db")
    st.append("league_configured", {"config": dict(DEFAULT_CFG)})
    st.set_meta("league_cfg", dict(DEFAULT_CFG))
    wa.store = st
    wa.engine = wa.replay_engine(st, wa.PLAYERS, wa.TrendAuction)
    yield st
    wa.store = None
    wa.engine = wa.TrendAuction([dict(p) for p in POOL_COVERING], teams=3, budget=100, io="IO")
    st.close()


def test_undo_persistito_riduce_le_vendite_attive(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=60, team="IO"))
    wa.api_sold(wa.SoldBody(key="A02", price=50, team="T1"))  # squadra diversa: nel budget
    assert wa.api_calibration()["n_sales"] == 2
    # undo persistito = revoke dell'ultima azione attiva -> motore ricostruito
    resp = wa.api_undo()
    assert resp.get("ok") is True
    data = wa.api_calibration()
    assert data["n_sales"] == 1
    # l'engine ricostruito dal replay espone solo gli eventi attivi
    kinds = [e["kind"] for e in wa.engine.events]
    assert kinds == ["sold"]
    assert wa.engine.state["sold"][0][1] == 60
    # l'advisory riflette la cella della sola vendita rimasta
    est = wa.api_eval(key="A03", team="IO")["calibration"]
    assert est["applied"] is False and est["n"] == 1


def test_correct_revoca_e_restate_riflette_gli_eventi_attivi(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=60, team="IO"))
    target = wa.store.last_action()["seq"]
    resp = wa.api_correct(wa.CorrectBody(target_seq=target, kind="restate", price=45, team="IO"))
    assert resp.get("ok") is True and resp["new"]["price"] == 45
    data = wa.api_calibration()
    assert data["n_sales"] == 1  # restate = 1 vendita attiva (revoke + nuova sold)
    sold = [e for e in wa.engine.events if e["kind"] == "sold"]
    assert sold[0]["price"] == 45 and sold[0]["premium_pfc"] == pytest.approx(0.9)
    # base 50 -> banda 50-100; 1 vendita attiva -> fase start
    cell = data["cells"]["A|50-100|start"]
    assert cell["n"] == 1 and cell["raw_center"] == pytest.approx(0.9)


def test_unsold_e_revoke_non_contano_nella_calibrazione(persisted):
    wa.api_sold(wa.SoldBody(key="A01", price=60, team="IO"))
    wa.api_sold(wa.SoldBody(key="A02", price=50, team="T1"))  # squadra diversa: nel budget
    wa.api_unsold(wa.NameBody(key="A03"))
    assert wa.api_calibration()["n_sales"] == 2  # unsold ignorato
    # revoke diretto (kind=revoke) rimuove una vendita dagli eventi attivi
    target = wa.store.last_action()["seq"]  # A03 unsold
    assert target is not None
    wa.api_correct(wa.CorrectBody(target_seq=target, kind="revoke"))
    assert wa.api_calibration()["n_sales"] == 2  # revoca dell'unsold: vendite invariate
    t2 = [e for e in wa.store.read_all() if e["type"] == "sold"][-1]["seq"]
    wa.api_correct(wa.CorrectBody(target_seq=t2, kind="revoke"))
    assert wa.api_calibration()["n_sales"] == 1


# ========================================================================
# input degli eventi: formato store e formato engine
# ========================================================================
def test_calibration_consuma_eventi_del_log_store():
    events = [
        store_sold_event(1, ruolo="A", base=50.0, price=60),  # premium 1.2
        store_sold_event(2, ruolo="A", base=50.0, price=55),  # premium 1.1
        {"seq": 3, "type": "unsold", "payload": {"pid": "A03", "ruolo": "A"}},
        {"seq": 4, "type": "revoke", "payload": {"target_seq": 1}, "supersedes": 1},
    ]
    rep = calibration.calibration_from_events(events, DEFAULT_CFG)
    assert rep.n_sales == 1  # unsold ignorata; il revoke (supersedes seq 1) ESCLUDE
    # la vendita seq 1: resta attiva solo la vendita seq 2 (premium 1.1)
    g = stats_of(rep)
    assert g["n"] == 1
    # premium derivato price/base anche senza premium_pfc (della vendita attiva)
    assert g["raw_center"] == pytest.approx(1.1)


def test_premium_fallback_price_base():
    e = {"kind": "sold", "i": 0, "ruolo": "A", "base": 40.0, "price": 60}
    rep = calibration.calibration_from_events([e], DEFAULT_CFG)
    assert stats_of(rep)["raw_center"] == pytest.approx(1.5)
