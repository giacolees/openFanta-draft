"""Test API (scripts/web_auction.py) e validazione configurazione del dominio.

Coprono il layer HTTP senza dipendere dal server reale: gli handler FastAPI
vengono invocati direttamente con i loro body pydantic, e il globale
`web_auction.engine` viene isolato per test tramite fixture (mai uvicorn).

Scenario richiesti:
- /api/sold e /api/unsold: vendita valida, oltre budget (409), squadra non
  tracciata (400), slot esaurito (409), doppia vendita (409), invenduto su
  venduto (409), prezzo non valido (400) — nessuna violazione dominio in 500.
- Team canonico negli eventi sold (squadra risolta: "IO"/"T1"/.../"ALTRO") e
  quindi in /api/trend; eventi e state['sold'] sempre allineati.
- Centralizzazione nel dominio della fattibilita' configurazione
  (Auction.validate_config): limits teams/budget, slots/formation, nomi e
  disponibilita' del listone per ruolo.
- POST /api/config: config valida sostituisce il motore; config impossibile
  per ruolo o nomi errati viene rifiutata (400) senza mutare lo stato.
"""

import json

import pytest  # pyright: ignore[reportMissingImports]
import web_auction as wa  # pyright: ignore[reportMissingImports]
from conftest import pid_of
from fastapi.responses import JSONResponse  # pyright: ignore[reportMissingImports]
from live_auction import (  # pyright: ignore[reportMissingImports]
    DEFAULTS,
    Auction,
    ConfigError,
)

DEFAULT_SLOTS = {"P": 3, "D": 3, "C": 3, "A": 6}
DEFAULT_FORMATION = {"P": 1, "D": 1, "C": 1, "A": 4}


def make_player(nome, ruolo="A", pfc=50.0, slot=2, tit=60.0):
    """Giocatore nel formato del listone (stesso schema di test_live_auction)."""
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


def set_engine(players, **overrides):
    """Sostituisce il motore globale di web_auction (isolamento per test).

    Quando i test passano slots ridotti (ma non la formation), la formation
    viene derivata in modo coerente (mai > slots): invariante strutturale di
    league_config, valida anche nel profilo engine del costruttore.
    """
    wa.PLAYERS = players
    overrides.setdefault("slots", dict(DEFAULT_SLOTS))
    overrides.setdefault(
        "formation",
        {
            r: min(DEFAULT_FORMATION[r], overrides["slots"][r])
            for r in DEFAULT_FORMATION
        },
    )
    wa.engine = wa.TrendAuction([dict(q) for q in players], **overrides)
    return wa.engine


def status_of(resp):
    """Status HTTP: i successi tornano dict (200), gli errori un JSONResponse."""
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def body_of(resp):
    return (
        json.loads(bytes(resp.body).decode("utf-8"))
        if isinstance(resp, JSONResponse)
        else resp
    )


def cfg_of(**over):
    """Configurazione candidate per validate_config, partendo dai DEFAULTS."""
    cfg = dict(DEFAULTS)
    cfg.update(over)
    return cfg


@pytest.fixture
def engine():
    """Motore fresco per ogni test: isola il globale senza dipendere da main()."""
    return set_engine(make_pool(a=8), teams=3, budget=100, io="IO")


POOL_FULL = make_pool(p=8, d=16, c=16, a=14)  # copre 2 squadre x (P3 D8 C8 A6)
POOL_NO_A = make_pool(p=8, d=16, c=16, a=4)  # A insufficiente per 2 squadre (12>4)


# ---------------------------------------------------------- nomina random
def test_random_nomination_progression_and_manual_override():
    players = make_pool(p=1, d=1, c=1, a=2)
    pids = [wa.compute_pid(p["nome"], p["ruolo"]) for p in players]
    eng = set_engine(
        players,
        teams=1,
        budget=100,
        auction_mode="random",
        random_queue=pids,
    )

    first = wa.api_nomination()
    assert first["mode"] == "random"
    assert first["current"]["pid"] == pids[0]
    assert first["remaining"] == len(pids)
    assert {eng.players[pid]["ruolo"] for pid in eng.nomination_queue()} == {
        "P",
        "D",
        "C",
        "A",
    }

    unsold = wa.api_unsold(wa.NameBody(key=pids[0]))
    assert unsold["nomination"]["current_pid"] == pids[1]
    assert eng.nomination_queue() == pids[1:] + [pids[0]]

    override = pids[3]
    sold = wa.api_sold(wa.SoldBody(key=override, price=1, team=None))
    assert sold["nomination"]["current_pid"] == pids[1]
    assert override not in eng.nomination_queue()


def test_manual_nomination_has_no_current_player(engine):
    data = wa.api_nomination()
    assert data == {
        "mode": "manual",
        "current": None,
        "remaining": len(engine.state["pool"]),
        "complete": False,
    }


def test_random_queue_must_match_listone():
    players = make_pool(a=3)
    with pytest.raises(ConfigError, match="esattamente tutti i pid"):
        set_engine(
            players,
            teams=1,
            budget=100,
            auction_mode="random",
            random_queue=[wa.compute_pid("A01", "A")],
        )


# -------------------------------------------------- viste mercato e rose
def test_api_svincolati_filtra_e_esclude_venduti():
    eng = set_engine(make_pool(p=1, d=1, c=1, a=3), teams=1, budget=100)
    sold_pid = pid_of(eng, "A01")
    wa.api_sold(wa.SoldBody(key=sold_pid, price=20, team="IO"))

    data = wa.api_svincolati(ruolo="A", q="A0", team="IO")
    assert data["count"] == 2
    assert {row["name"] for row in data["rows"]} == {"A02", "A03"}
    assert sold_pid not in {row["pid"] for row in data["rows"]}
    assert all(row["role"] == "A" for row in data["rows"])
    assert all("suggested_price" in row and "scarcity" in row for row in data["rows"])
    assert status_of(wa.api_svincolati(ruolo="X")) == 400


def test_api_svincolati_ordina_e_pagina_prima_della_valutazione(monkeypatch):
    players = [
        make_player("LOW", pfc=10),
        make_player("HIGH", pfc=50),
        make_player("MID", pfc=30),
    ]
    eng = set_engine(players, teams=1, budget=100)

    def forbidden_evaluate(*_args, **_kwargs):
        raise AssertionError(
            "la lista mercato non deve calcolare il breakdown completo"
        )

    monkeypatch.setattr(eng, "evaluate", forbidden_evaluate)
    data = wa.api_svincolati(
        ruolo="A", sort_by="pma", direction="desc", offset=1, limit=2
    )
    assert data["count"] == 3
    assert data["page_count"] == 2
    assert [row["name"] for row in data["rows"]] == ["MID", "LOW"]
    assert data["sort_by"] == "pma" and data["direction"] == "desc"


def test_api_svincolati_rifiuta_ordinamento_e_paginazione_invalidi():
    set_engine(make_pool(a=3), teams=1, budget=100)
    assert status_of(wa.api_svincolati(sort_by="unknown")) == 400
    assert status_of(wa.api_svincolati(direction="sideways")) == 400
    assert status_of(wa.api_svincolati(offset=-1)) == 400
    assert status_of(wa.api_svincolati(limit=201)) == 400


def test_api_rose_espone_acquisti_e_riepilogo_squadre():
    eng = set_engine(make_pool(a=4), teams=2, budget=100, io="IO")
    wa.api_sold(wa.SoldBody(key="A01", price=30, team="IO"))
    wa.api_sold(wa.SoldBody(key="A02", price=20, team="T1"))

    data = wa.api_rose(team="IO", ruolo="A")
    assert data["count"] == 1
    assert data["rows"][0]["player"] == "A01"
    assert data["rows"][0]["price"] == 30
    own = next(item for item in data["summaries"] if item["team"] == "IO")
    assert own["spent"] == 30
    assert own["remaining_budget"] == 70
    assert own["purchases"] == 1
    assert own["filled_slots"]["A"] == 1
    assert own["total_slots"]["A"] == eng.cfg["slots"]["A"]
    assert status_of(wa.api_rose(team="INESISTENTE")) == 400
    assert status_of(wa.api_rose(ruolo="X")) == 400


def test_api_rose_delete_in_memory_rimborsa_e_preserva_undo():
    eng = set_engine(make_pool(a=4), teams=2, budget=100, io="IO")
    a01 = pid_of(eng, "A01")
    a02 = pid_of(eng, "A02")
    wa.api_sold(wa.SoldBody(key=a01, price=30, team="IO"))
    wa.api_sold(wa.SoldBody(key=a02, price=20, team="T1"))

    resp = wa.api_rose_delete(a01)

    assert status_of(resp) == 200
    assert resp["removed"] == {
        "pid": a01,
        "player": "A01",
        "team": "IO",
        "price": 30,
    }
    assert wa.engine.state["sold"] == [(a02, 20, "T1", "A")]
    assert wa.engine.state["money"]["IO"] == 100
    assert wa.engine.state["slots"]["IO"]["A"] == eng.cfg["slots"]["A"]
    assert a01 in wa.engine.state["pool"]
    assert status_of(wa.api_rose_delete(a01)) == 404

    # Gli snapshot sono rigenerati: undo agisce sull'ultima vendita rimasta.
    assert body_of(wa.api_undo())["ok"] is True
    assert wa.engine.state["sold"] == []


# ----------------------------------------------------------- vendita valida
def test_api_vendita_valida_aggiorna_stato(engine):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))
    assert status_of(resp) == 200
    body = body_of(resp)
    assert body["ok"] is True and body["team"] == "IO"
    assert body["pid"] == pid_of(engine, "A01")
    assert engine.state["money"]["IO"] == 60
    assert engine.state["money_league"] == 3 * 100 - 40
    assert pid_of(engine, "A01") not in engine.state["pool"]
    assert engine.state["sold"] == [(pid_of(engine, "A01"), 40, "IO", "A")]
    assert engine.state["spent_unknown"] == 0


def test_api_vendita_altro_canonico(engine):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=40, team=None))
    assert status_of(resp) == 200
    assert body_of(resp)["team"] == "ALTRO"  # None -> ALTRO nel body e nello stato
    assert engine.state["sold"][0][2] == "ALTRO"
    assert engine.state["spent_unknown"] == 40


# ------------------------------------------------------- oltre budget (409)
def test_api_vendita_oltre_budget_409(engine):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=101, team="IO"))
    assert status_of(resp) == 409
    assert "budget" in body_of(resp)["error"].lower()
    assert pid_of(engine, "A01") in engine.state["pool"]  # nessuna mutazione
    assert engine.state["money"]["IO"] == 100
    assert engine.state["sold"] == []


# ------------------------------------------------ squadra non tracciata (400)
def test_api_squadra_non_tracciata_400(engine):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=40, team="ROMA"))
    assert status_of(resp) == 400  # input invalido, non conflitto di stato
    assert "ALTRO" in body_of(resp)["error"]
    assert pid_of(engine, "A01") in engine.state["pool"]
    assert engine.state["spent_unknown"] == 0


# ------------------------------------------------ slot esaurito (409)
def test_api_slot_esaurito_409():
    set_engine(
        make_pool(a=3), teams=1, budget=50, slots={"P": 3, "D": 3, "C": 3, "A": 1}
    )
    assert status_of(wa.api_sold(wa.SoldBody(key="A01", price=10, team="IO"))) == 200
    resp = wa.api_sold(wa.SoldBody(key="A02", price=10, team="IO"))
    assert status_of(resp) == 409
    assert "slot" in body_of(resp)["error"].lower()
    assert wa.engine.state["money"]["IO"] == 40  # solo la prima vendita
    assert wa.engine.state["slots"]["IO"]["A"] == 0


# ------------------------------------------------------ doppia vendita (409)
def test_api_doppia_vendita_409(engine):
    assert status_of(wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))) == 200
    resp = wa.api_sold(wa.SoldBody(key="A01", price=30, team="T1"))
    assert status_of(resp) == 409
    assert "venduto" in body_of(resp)["error"]
    assert len(engine.state["sold"]) == 1
    assert engine.state["money"]["T1"] == 100  # altra squadra intatta


# ------------------------------------------- invenduto su venduto (409)
def test_api_invenduto_su_venduto_409(engine):
    assert status_of(wa.api_sold(wa.SoldBody(key="A01", price=40, team="IO"))) == 200
    resp = wa.api_unsold(wa.NameBody(key="A01"))
    assert status_of(resp) == 409
    assert "pool" in body_of(resp)["error"]
    assert engine.state["unsold"].get(pid_of(engine, "A01"), 0) == 0
    # invenduto valido su giocatore ancora in carta
    assert status_of(wa.api_unsold(wa.NameBody(key="A02"))) == 200
    assert engine.state["unsold"][pid_of(engine, "A02")] == 1


# ----------------------------------------------------- prezzo non valido (400)
def test_api_prezzo_non_valido_400(engine):
    resp = wa.api_sold(wa.SoldBody(key="A01", price=0, team="IO"))
    assert status_of(resp) == 400
    assert pid_of(engine, "A01") in engine.state["pool"]
    assert engine.state["sold"] == []


# --------------------------------------- team canonico negli eventi /api/trend
def test_api_trend_evento_con_team(engine):
    # minuscolo -> squadra canonica IO; None -> ALTRO esplicito
    assert status_of(wa.api_sold(wa.SoldBody(key="A01", price=40, team="io"))) == 200
    assert status_of(wa.api_sold(wa.SoldBody(key="A02", price=30, team=None))) == 200
    data = wa.api_trend()
    sold = [e for e in data["events"] if e["kind"] == "sold"]
    assert sold[0]["team"] == "IO"
    assert sold[1]["team"] == "ALTRO"
    # allineamento eventi/state: stesso prezzo e stesso team nello stato
    assert sold[0]["price"] == engine.state["sold"][0][1]
    assert sold[0]["team"] == engine.state["sold"][0][2]
    assert sold[1]["price"] == engine.state["sold"][1][1]
    assert sold[1]["team"] == engine.state["sold"][1][2]
    assert set(data["verdict_labels"]) == {"pfc", "pma"}


def test_trend_undo_eventi_e_stato_allineati():
    eng = set_engine(make_pool(a=4), teams=2, budget=100, io="IO")
    eng.mark_sold(eng.find("A01"), 40, "IO", eng.evaluate(eng.find("A01"), "IO"))
    eng.mark_unsold(eng.find("A02"))
    assert [e["kind"] for e in eng.events] == ["sold", "unsold"]
    assert [e.get("team") for e in eng.events] == ["IO", None]
    assert [e.get("pid") for e in eng.events] == [
        pid_of(eng, "A01"),
        pid_of(eng, "A02"),
    ]
    assert eng.undo()
    assert [e["kind"] for e in eng.events] == ["sold"]
    assert len(eng.state["sold"]) == 1
    assert eng.undo()
    assert eng.events == [] and eng.state["sold"] == []
    assert pid_of(eng, "A01") in eng.state["pool"]


# --------------------------------------------- dominio: squadra canonica
def test_domain_mark_sold_restituisce_squadra_canonica():
    eng = Auction(make_pool(a=4), teams=2, budget=100, io="IO")
    assert eng.mark_sold(eng.find("A01"), 40, "io") == "IO"
    assert eng.mark_sold(eng.find("A02"), 30, None) == "ALTRO"  # ALTRO esplicito
    assert eng.mark_sold(eng.find("A03"), 20, "altro") == "ALTRO"
    assert [s[2] for s in eng.state["sold"]] == ["IO", "ALTRO", "ALTRO"]


# ------------------------------------ dominio: validazione configurazione
def test_domain_config_valida():
    # 2 squadre x (P3 D8 C8 A6): il pool copre ogni ruolo
    Auction.validate_config(POOL_FULL, cfg_of(teams=2, budget=300))  # nessuna eccezione


def test_domain_config_impossibile_per_ruolo():
    with pytest.raises(ConfigError, match="Attaccanti"):
        Auction.validate_config(POOL_NO_A, cfg_of(teams=2, budget=300))


def test_domain_config_limiti_teams_e_budget():
    with pytest.raises(ConfigError, match="squadre"):
        Auction.validate_config(POOL_FULL, cfg_of(teams=1))
    with pytest.raises(ConfigError, match="budget"):
        Auction.validate_config(POOL_FULL, cfg_of(teams=2, budget=5))


def test_domain_config_nomi_duplicati_o_io_assente():
    with pytest.raises(ConfigError, match="nomi"):
        Auction.validate_config(
            POOL_FULL, cfg_of(teams=2, team_names=["IO", "IO"], io="IO")
        )
    with pytest.raises(ConfigError, match="nomi"):
        Auction.validate_config(POOL_FULL, cfg_of(teams=2, team_names=["IO"], io="IO"))
    with pytest.raises(ConfigError, match="IO"):
        Auction.validate_config(
            POOL_FULL, cfg_of(teams=2, team_names=["T1", "T2"], io="IO")
        )


def test_domain_config_formazione_oltre_slot():
    with pytest.raises(ConfigError, match="formazione"):
        Auction.validate_config(
            POOL_FULL, cfg_of(teams=2, formation={"P": 4, "D": 8, "C": 8, "A": 6})
        )


# ------------------------------------------------------------ /api/config
def test_api_config_valida_sostituisce_il_motore():
    set_engine(make_pool(a=6), teams=3, budget=100, io="IO")
    before = wa.engine
    wa.PLAYERS = POOL_FULL
    resp = wa.api_config_post(
        wa.ConfigBody(teams=2, budget=300, names=["IO", "T1"], io="IO")
    )
    assert status_of(resp) == 200
    body = body_of(resp)
    assert body["ok"] is True and body["teams"] == 2 and body["budget"] == 300
    assert wa.engine is not before  # motore ricreato
    assert wa.engine.cfg["team_names"] == ["IO", "T1"]
    assert wa.engine.state["sold"] == [] and wa.engine.events == []


def test_api_config_impossibile_per_ruolo_400_preserva_stato():
    set_engine(make_pool(a=4), teams=3, budget=100, io="IO")
    wa.api_sold(wa.SoldBody(key="A02", price=40, team="IO"))  # stato non banale
    before = wa.engine
    wa.PLAYERS = POOL_NO_A
    resp = wa.api_config_post(
        wa.ConfigBody(teams=2, budget=300, names=["IO", "T1"], io="IO")
    )
    assert status_of(resp) == 400
    assert "Attaccanti" in body_of(resp)["error"]
    assert wa.engine is before  # motore non sostituito
    assert wa.engine.state["sold"] == [(pid_of(wa.engine, "A02"), 40, "IO", "A")]
    assert wa.engine.state["money"]["IO"] == 60
    assert [e["kind"] for e in wa.engine.events] == ["sold"]


def test_api_config_nomi_errati_400():
    set_engine(make_pool(a=6), teams=3, budget=100, io="IO")
    before = wa.engine
    resp = wa.api_config_post(wa.ConfigBody(teams=2, budget=300, names=["IO"], io="IO"))
    assert status_of(resp) == 400
    assert "nomi" in body_of(resp)["error"].lower()
    assert wa.engine is before
