"""Test dello schema configurazione lega condiviso (WP2: scripts/league_config.py).

Coprono il contratto unico usato da dominio (Auction / validate_config), CLI
(live_auction.config_overrides/config_errors) e web (ConfigBody, POST /api/config):

- normalizzazione: chiavi parziali -> config completa (defaults, nomi squadra,
  slot/formation per ruolo), idempotente, tuning preservato.
- validate(): errori strutturali, con i DUE profili documentati — "public"
  (API: 2-20 squadre, 10-10000 crediti) e "engine" (costruttore del motore:
  teams >= 1, budget >= 1, pool parziale). Le invarianti strutturali (tra cui
  formation <= slot) valgono identiche in entrambi.
- minimum_roster_cost(): costo minimo completamento rosa (floor min_slot_price,
  hook role_floor_price per WP6).
- feasibility(): errori (pool per ruolo, budget vs costo minimo) + warning
  (margine zero, budget rigido); mai eseguita nel profilo engine.
- Parita' dei tre canali e "config rifiutata = nessun reset" (CLI e web).
"""

import json

import league_config  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import pid_of
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingImports]
from live_auction import (  # pyright: ignore[reportMissingImports]
    Auction,
    ConfigError,
    config_errors,
    config_overrides,
    proposed_cfg,
    run_repl,
)

ROLE_ORDER = ["P", "D", "C", "A"]


def make_player(nome, ruolo="A", pfc=50.0, slot=2, tit=60.0):
    """Giocatore nel formato del listone (stesso schema di conftest/test_web)."""
    return {
        "nome": nome,
        "squadra": "SQ",
        "ruolo": ruolo,
        "pfc": float(pfc),
        "pma": float(pfc),
        "pfc_range": "",
        "pma_range": "",
        "dpfcpma": 0.0,
        "slot": slot,
        "tit": float(tit),
        "expfm": 6.5,
        "fascia": "Top",
        "status": "T",
        "pen_prob": 0.0,
        "fk_prob": 0.0,
        "tix": 50.0,
        "fix": 50.0,
        "fix_contrib": 0.3,
    }


def make_pool(p=0, d=0, c=0, a=0):
    """Pool di giocatori con nomi per ruolo (P01.., D01.., C01.., A01..)."""
    players = []
    for role, n in (("P", p), ("D", d), ("C", c), ("A", a)):
        players += [make_player(f"{role}{i:02d}", ruolo=role) for i in range(1, n + 1)]
    return players


POOL_FULL = make_pool(p=10, d=20, c=20, a=14)  # 2 squadre x (P3 D8 C8 A6) con margine
POOL_FULL_STRETCH = make_pool(p=8, d=16, c=16, a=14)  # margine zero su D e C
POOL_NO_A = make_pool(p=8, d=16, c=16, a=4)  # A insufficiente (12 > 4)


def cfg_of(**over):
    """Configurazione candidata dai DEFAULTS (stesso helper di test_web_auction)."""
    cfg = dict(league_config.DEFAULTS)
    cfg.update(over)
    return cfg


# ------------------------------------------------------------ normalizzazione
def test_normalize_completa_defaults():
    n = league_config.normalize({"teams": 8})
    assert n["teams"] == 8 and n["budget"] == 500
    assert n["slots"] == {"P": 3, "D": 8, "C": 8, "A": 6}
    assert n["formation"] == {"P": 1, "D": 4, "C": 4, "A": 2}
    assert n["tit_cov_threshold"] == 70
    assert n["team_names"] == ["IO"] + [f"T{i}" for i in range(1, 8)]


def test_normalize_slots_parziali_per_ruolo():
    n = league_config.normalize({"teams": 4, "slots": {"P": 2}})
    assert n["slots"] == {"P": 2, "D": 8, "C": 8, "A": 6}
    n2 = league_config.normalize({"teams": 4, "formation": {"A": 3}})
    assert n2["formation"] == {"P": 1, "D": 4, "C": 4, "A": 3}


def test_normalize_nomi_derivati_io_in_testa():
    n = league_config.normalize({"teams": 2, "io": "  MIA  "})
    assert n["io"] == "MIA"
    assert n["team_names"] == ["MIA", "T1"]


def test_normalize_rispetta_nomi_forniti_e_strip():
    n = league_config.normalize({"teams": 2, "team_names": [" IO ", "T1"], "io": "IO"})
    assert n["team_names"] == ["IO", "T1"]


def test_normalize_preserva_chiavi_tuning():
    n = league_config.normalize({"teams": 8, "aggression": 1.5, "scarcity_max": 4.0})
    assert n["aggression"] == 1.5 and n["scarcity_max"] == 4.0


def test_normalize_idempotente():
    n1 = league_config.normalize({"teams": 3, "budget": 200})
    assert league_config.normalize(n1) == n1


# ------------------------------------------------------------------- validate
def test_validate_pubblico_range_teams_e_budget():
    errs = league_config.validate(league_config.normalize({"teams": 1}))
    assert any("squadre" in e for e in errs)
    errs = league_config.validate(league_config.normalize({"teams": 2, "budget": 5}))
    assert any("budget" in e for e in errs)


def test_validate_formazione_oltre_slot():
    errs = league_config.validate(
        league_config.normalize(
            {"teams": 2, "formation": {"P": 4, "D": 8, "C": 8, "A": 6}}
        )
    )
    assert any("formazione" in e for e in errs)


def test_validate_nomi_duplicati_io_e_conteggio():
    errs = league_config.validate(
        league_config.normalize({"teams": 2, "team_names": ["IO", "IO"], "io": "IO"})
    )
    assert any("nomi" in e for e in errs)
    errs = league_config.validate(
        league_config.normalize({"teams": 2, "team_names": ["IO"], "io": "IO"})
    )
    assert any("nomi" in e and "1" in e for e in errs)
    errs = league_config.validate(
        league_config.normalize({"teams": 2, "team_names": ["T1", "T2"], "io": "IO"})
    )
    assert any("IO" in e for e in errs)


def test_validate_tit_cov_threshold_opzionale():
    # assente = opzionale (default 70 in normalize); fuori range = errore
    norm = league_config.normalize({"teams": 2})
    assert (
        league_config.validate(
            {k: v for k, v in norm.items() if k != "tit_cov_threshold"}
        )
        == []
    )
    errs = league_config.validate(dict(norm, tit_cov_threshold=150))
    assert any("tit_cov_threshold" in e for e in errs)


def test_validate_slots_non_dict_e_slot_negativo():
    errs = league_config.validate(league_config.normalize({"teams": 2, "slots": [3]}))
    assert any("slots" in e for e in errs)
    errs = league_config.validate(
        league_config.normalize({"teams": 2, "slots": {"P": -1}})
    )
    assert any("slot" in e and "P" in e for e in errs)


def test_profilo_engine_accetta_la_config_dei_test_sintetici():
    """Profilo engine (costruttore): squadra singola e pool piccolo ammessi per
    simulazioni; le invarianti strutturali (formation <= slots) valgono comunque."""
    cfg = {
        "teams": 1,
        "budget": 100,
        "slots": {"P": 3, "D": 3, "C": 3, "A": 6},
        # formation coerente: la configurazione sintetica e' VALIDA, non alla
        # deroga dell'invariante (che resta in ogni profilo)
        "formation": {"P": 1, "D": 1, "C": 1, "A": 4},
    }
    assert league_config.validate(league_config.normalize(cfg), profile="engine") == []
    # l'API pubblica rifiuta la stessa config (teams=1): il profilo engine non
    # indebolisce l'API pubblica 2-20
    assert any(
        "squadre" in e for e in league_config.validate(league_config.normalize(cfg))
    )
    # e il costruttore del motore la accetta (parita' profili)
    auction = Auction(
        make_pool(a=8),
        teams=1,
        budget=100,
        slots={"P": 3, "D": 3, "C": 3, "A": 6},
        formation={"P": 1, "D": 1, "C": 1, "A": 4},
    )
    assert auction.cfg["teams"] == 1
    assert auction.cfg["formation"] == {"P": 1, "D": 1, "C": 1, "A": 4}


def test_invariante_formazione_oltre_slot_vale_anche_nel_profilo_engine():
    """formation <= slots e' un'invariante strutturale: rifiutata anche dal
    costruttore del motore, non solo dall'API pubblica."""
    cfg = {
        "teams": 1,
        "budget": 100,
        "slots": {"P": 3, "D": 3, "C": 3, "A": 6},
        "formation": {"P": 1, "D": 4, "C": 4, "A": 6},  # D/C: 4 > 3 slot
    }
    errs = league_config.validate(league_config.normalize(cfg), profile="engine")
    assert any("formazione" in e for e in errs)
    with pytest.raises(ConfigError, match="formazione"):
        Auction(
            make_pool(a=8),
            teams=1,
            budget=100,
            slots={"P": 3, "D": 3, "C": 3, "A": 6},
            formation={"P": 1, "D": 4, "C": 4, "A": 2},
        )


# ------------------------------------------------------------------- costi
def test_minimum_roster_cost_default():
    n = league_config.normalize({"teams": 2})
    assert league_config.minimum_roster_cost(n) == 25 * 2  # 3+8+8+6 slot x floor 2


def test_minimum_roster_cost_con_pool_limitato():
    n = league_config.normalize({"teams": 2})
    players = make_pool(p=4, d=5, c=20, a=20)  # P e D scarsi
    # min(slots, disponibili): P 3->3, D 8->5, C 8, A 6 = 22 slot x 2 = 44
    assert league_config.minimum_roster_cost(n, players) == 44


def test_minimum_roster_cost_floor_per_ruolo_wp6():
    n = league_config.normalize({"teams": 2, "role_floor_price": {"A": 3}})
    # base 25 slot x 2 = 50, ma A (6 slot) costa 3 -> 50 + 6 = 56
    assert league_config.minimum_roster_cost(n) == 56


def test_minimum_roster_cost_usa_floor_min_slot_price():
    n = league_config.normalize({"teams": 2, "min_slot_price": 5})
    assert league_config.minimum_roster_cost(n) == 25 * 5


# ------------------------------------------------- WP6: floor e pesi validati
def test_validate_role_floor_price_ok_e_malformato():
    # parziali ok (ruoli mancanti -> fallback min_slot_price)
    n = league_config.normalize({"teams": 2, "role_floor_price": {"P": 3, "A": 4}})
    assert league_config.validate(n) == []
    # floor < 1, ruolo sconosciuto, non numerico
    n0 = league_config.normalize({"teams": 2, "role_floor_price": {"P": 0}})
    assert any("floor" in e and "P" in e for e in league_config.validate(n0))
    nx = league_config.normalize({"teams": 2, "role_floor_price": {"X": 2}})
    assert any("ruolo sconosciuto" in e for e in league_config.validate(nx))
    ns = league_config.normalize({"teams": 2, "role_floor_price": {"P": "due"}})
    assert any("floor" in e and "P" in e for e in league_config.validate(ns))
    nd = league_config.normalize({"teams": 2, "role_floor_price": [3, 2]})
    assert any("role_floor_price" in e for e in league_config.validate(nd))


def test_validate_role_budget_weights_ok_e_malformato():
    n = league_config.normalize(
        {"teams": 2, "role_budget_weights": {"P": 2, "D": 1, "C": 1, "A": 3}}
    )
    assert league_config.validate(n) == []
    # somma zero: vietata
    n0 = league_config.normalize(
        {"teams": 2, "role_budget_weights": {"P": 0, "D": 0, "C": 0, "A": 0}}
    )
    assert any("somma" in e for e in league_config.validate(n0))
    # peso negativo, ruolo sconosciuto, non dict
    nn = league_config.normalize(
        {"teams": 2, "role_budget_weights": {"P": -1, "D": 1, "C": 1, "A": 1}}
    )
    assert any("peso" in e and "P" in e for e in league_config.validate(nn))
    nx = league_config.normalize({"teams": 2, "role_budget_weights": {"X": 1}})
    assert any("ruolo sconosciuto" in e for e in league_config.validate(nx))
    nd = league_config.normalize({"teams": 2, "role_budget_weights": [1, 2]})
    assert any("role_budget_weights" in e for e in league_config.validate(nd))


def test_validate_wp6_chiavi_assenti_backward_compat():
    # l'assenza delle chiavi WP6 non produce errori (backward compat)
    assert (
        league_config.validate(league_config.normalize({"teams": 2, "budget": 300}))
        == []
    )


# ---------------------------------------------------------------- feasibility
def test_feasibility_ok_silenzioso():
    cfg = league_config.normalize({"teams": 2, "budget": 300})
    f = league_config.feasibility(cfg, POOL_FULL)
    assert f.ok and f.all == []


def test_feasibility_errore_pool_insufficiente():
    cfg = league_config.normalize({"teams": 2, "budget": 300})
    f = league_config.feasibility(cfg, POOL_NO_A)
    assert not f.ok
    assert any("Attaccanti" in e for e in f.errors)


def test_feasibility_budget_insufficiente():
    cfg = league_config.normalize({"teams": 2, "budget": 40})  # costo minimo 50
    f = league_config.feasibility(cfg, POOL_FULL)
    assert any("budget" in e for e in f.errors)


def test_feasibility_warning_budget_rigido():
    cfg = league_config.normalize({"teams": 2, "budget": 60})  # costo minimo 50: 83%
    f = league_config.feasibility(cfg, POOL_FULL)
    assert f.ok
    assert any("budget rigido" in w for w in f.warnings)


def test_feasibility_warning_margine_zero():
    cfg = league_config.normalize({"teams": 2, "budget": 300})
    f = league_config.feasibility(cfg, POOL_FULL_STRETCH)  # D e C esattamente coperti
    assert f.ok
    assert any("margine zero" in w and "Difensori" in w for w in f.warnings)
    assert any("margine zero" in w and "Centrocampisti" in w for w in f.warnings)
    assert not any("margine zero" in w and "Attaccanti" in w for w in f.warnings)


def test_feasibility_profilo_engine_vuota():
    cfg = league_config.normalize({"teams": 2, "budget": 40})
    f = league_config.feasibility(cfg, POOL_NO_A, profile="engine")
    assert f.ok and f.errors == [] and f.warnings == []


# ------------------------------------------------------- wrapper e parita'
def test_validate_config_wrapper_delega_al_modulo():
    cfg = {"teams": 1, "budget": 300, "names": ["IO", "T1"], "io": "IO"}
    norm_cfg = league_config.normalize(cfg)
    expected = list(league_config.validate(norm_cfg))
    expected += league_config.feasibility(norm_cfg, POOL_FULL).errors
    with pytest.raises(ConfigError) as exc:
        Auction.validate_config(POOL_FULL, cfg)
    assert str(exc.value) == "; ".join(expected)


def test_validate_config_wrapper_valida_e_non_muta():
    before = {"teams": 2, "budget": 300, "names": ["IO", "T1"], "io": "IO"}
    Auction.validate_config(POOL_FULL, dict(before))  # nessuna eccezione
    assert before == {"teams": 2, "budget": 300, "names": ["IO", "T1"], "io": "IO"}


def test_parita_teams_singolo_cli_web_domain():
    players = make_pool(a=8)
    cfg = {"teams": 1, "budget": 100, "names": ["IO"], "io": "IO"}
    # dominio
    with pytest.raises(ConfigError) as exc:
        Auction.validate_config(players, cfg)
    assert "squadre" in str(exc.value)
    # CLI (stesso validatore del comando config del REPL)
    auction = Auction(players, teams=3, budget=100)
    cli_errors = config_errors(auction, {"teams": 1})
    assert any("squadre" in e for e in cli_errors)
    # web
    wa.PLAYERS = players
    resp = wa.api_config_post(wa.ConfigBody(teams=1, budget=100, names=["IO"], io="IO"))
    assert _status_of(resp) == 400
    assert "squadre" in _body_of(resp)["error"]


def test_parita_pool_insufficiente_cli_web_domain():
    players = POOL_NO_A
    cfg = cfg_of(teams=2, budget=300)
    with pytest.raises(ConfigError, match="Attaccanti"):
        Auction.validate_config(players, cfg)
    auction = Auction(players, teams=3, budget=100)
    cli_errors = config_errors(
        auction, league_config.normalize(cfg_of(teams=2, budget=300))
    )
    assert any("Attaccanti" in e for e in cli_errors)
    wa.PLAYERS = players
    resp = wa.api_config_post(
        wa.ConfigBody(teams=2, budget=300, names=["IO", "T1"], io="IO")
    )
    assert _status_of(resp) == 400
    assert "Attaccanti" in _body_of(resp)["error"]


# ---------------------------------------------------------------------- CLI
def test_cli_config_rifiutata_non_resetta(monkeypatch, capsys):
    """Il comando config del REPL valida PRIMA: su errore stampa la lista errori
    e l'asta corrente resta intatta (niente reset)."""
    auction = Auction(make_pool(a=8), teams=3, budget=100)
    auction.mark_sold(auction.find("A01"), 40, "IO")  # stato non banale
    lines = iter(["config squadre=1", "stato", "esci"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    run_repl(auction)
    out = capsys.readouterr().out
    assert "Errore: il numero di squadre" in out
    assert "Configurazione aggiornata" not in out
    assert "T2 100" in out  # la lega da 3 squadre x 100 cr e' ancora quella
    assert auction.state["sold"] == [
        (pid_of(auction, "A01"), 40, "IO", "A")
    ]  # niente reset


def test_cli_config_rifiutata_per_pool_non_resetta(monkeypatch, capsys):
    """Serve anche la fattibilita': una config con pool insufficiente per ruolo
    non azzera (stesso flusso del web: errori = 400 / niente reset)."""
    auction = Auction(make_pool(a=4), teams=3, budget=100)
    auction.mark_sold(auction.find("A01"), 40, "IO")
    lines = iter(["config squadre=2 slota=6", "esci"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    run_repl(auction)
    out = capsys.readouterr().out
    assert "Attaccanti" in out
    assert "Configurazione aggiornata" not in out
    assert auction.state["sold"] == [(pid_of(auction, "A01"), 40, "IO", "A")]


def test_cli_config_valida_resetta(monkeypatch, capsys):
    auction = Auction(make_pool(p=9, d=24, c=24, a=18), teams=3, budget=100)
    lines = iter(["config budget=200", "esci"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    run_repl(auction)
    out = capsys.readouterr().out
    assert "Configurazione aggiornata" in out
    assert "IO 200" in out  # nuovo budget applicato (3 squadre, non i DEFAULTS)


def test_config_overrides_token_invalidi():
    auction = Auction(make_pool(a=8), teams=3, budget=100)
    assert config_overrides(auction, ["budget=200"]) == {"budget": 200}
    assert config_overrides(auction, ["nonsenso"]) is None
    assert config_overrides(auction, ["budget=abc"]) is None
    # slotp=2 parte dagli slot correnti del motore e aggiorna solo il ruolo P
    ov = config_overrides(auction, ["slotp=2"])
    assert set(ov) == {"slots"} and ov["slots"]["P"] == 2
    assert ov["slots"]["D"] == 8  # ruoli non toccati preservati


def test_proposed_cfg_parte_dalla_config_corrente():
    auction = Auction(make_pool(p=9, d=24, c=24, a=18), teams=3, budget=100)
    assert config_errors(auction, {"budget": 200}) == []  # 3 squadre, pool ok
    prop = proposed_cfg(auction, {"budget": 200})
    assert prop["teams"] == 3 and prop["budget"] == 200  # non i DEFAULTS
    assert prop["team_names"] == ["IO", "T1", "T2"]


# --------------------------------------------------------------------- web
def _set_engine(players, **overrides):
    wa.PLAYERS = players
    wa.engine = wa.TrendAuction([dict(q) for q in players], **overrides)
    return wa.engine


def _status_of(resp):
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _body_of(resp):
    return (
        json.loads(bytes(resp.body).decode("utf-8"))
        if isinstance(resp, JSONResponse)
        else resp
    )


def test_web_config_post_estesa_slots_formation_tit():
    _set_engine(POOL_FULL, teams=2, budget=300)
    before = wa.engine
    resp = wa.api_config_post(
        wa.ConfigBody(
            teams=2,
            budget=300,
            names=["IO", "T1"],
            io="IO",
            slots={"P": 3, "D": 4, "C": 4, "A": 4},
            formation={"P": 1, "D": 2, "C": 2, "A": 1},
            tit_cov_threshold=75,
        )
    )
    assert _status_of(resp) == 200
    body = _body_of(resp)
    assert body["ok"] is True
    cfg = body["config"]
    assert cfg["teams"] == 2 and cfg["budget"] == 300
    assert cfg["slots"] == {"P": 3, "D": 4, "C": 4, "A": 4}
    assert cfg["formation"] == {"P": 1, "D": 2, "C": 2, "A": 1}
    assert cfg["tit_cov_threshold"] == 75
    assert body["feasibility"] == {"errors": [], "warnings": []}
    assert body["minimum_roster_cost"] == (3 + 4 + 4 + 4) * 2
    assert wa.engine is not before  # motore ricreato solo su config accettata
    assert wa.engine.cfg["tit_cov_threshold"] == 75
    assert wa.engine.state["sold"] == [] and wa.engine.events == []
    # compat: chiavi legacy in testa alla risposta
    assert body["teams"] == 2 and body["budget"] == 300
    assert body["names"] == ["IO", "T1"] and body["io"] == "IO"


def test_web_config_get_restituisce_config_normalizzata():
    _set_engine(make_pool(a=8), teams=3, budget=100)
    body = wa.api_config()
    assert set(body) == {
        "teams",
        "budget",
        "names",
        "io",
        "slots",
        "formation",
        "tit_cov_threshold",
        # WP8: flag advisory esposto (non applicato al prezzo in questo WP)
        "use_calibration_in_price",
    }
    assert body["slots"] == {"P": 3, "D": 8, "C": 8, "A": 6}  # default league_config
    assert body["names"] == ["IO", "T1", "T2"]
    assert body["use_calibration_in_price"] is False  # default advisory OFF


def test_web_config_formazione_oltre_slot_400_no_reset():
    _set_engine(make_pool(a=8), teams=3, budget=100)
    wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))  # stato non banale
    before = wa.engine
    resp = wa.api_config_post(
        wa.ConfigBody(
            teams=2,
            budget=300,
            names=["IO", "T1"],
            io="IO",
            slots={"P": 3, "D": 2, "C": 8, "A": 6},
            formation={"P": 1, "D": 4, "C": 4, "A": 2},  # D: 4 > 2 slot
        )
    )
    assert _status_of(resp) == 400
    assert "formazione" in _body_of(resp)["error"]
    assert wa.engine is before  # motore non sostituito
    assert wa.engine.state["sold"] == [(pid_of(wa.engine, "A01"), 40, "IO", "A")]


def test_web_config_pool_insufficiente_400_no_reset():
    _set_engine(make_pool(a=4), teams=3, budget=100)
    wa.api_sold(wa.SoldBody(key="A02", price=40, team="IO"))
    before = wa.engine
    wa.PLAYERS = POOL_NO_A
    resp = wa.api_config_post(
        wa.ConfigBody(teams=2, budget=300, names=["IO", "T1"], io="IO")
    )
    assert _status_of(resp) == 400
    assert "Attaccanti" in _body_of(resp)["error"]
    assert wa.engine is before  # motore preservato
    assert wa.engine.state["sold"] == [(pid_of(wa.engine, "A02"), 40, "IO", "A")]
    assert wa.engine.state["money"]["IO"] == 60
