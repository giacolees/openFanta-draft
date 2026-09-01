"""Test unitari del motore di dominio dell'asta (scripts/live_auction.py).

Coprono le invarianti centralizzate nella vendita (player nel pool, prezzo
intero positivo, squadra valida o ALTRO esplicito, budget sufficiente, slot
del ruolo disponibile), la robustezza di domanda/scarsità sotto stati
anomali (vendite ALTRO oltre il tetto, slot iniziali zero, pool vuoto), il
deep-copy dei player del chiamante e il validator ``check_invariants``
(incluse le corruzioni intenzionali che deve rilevare).

Infrastruttura condivisa (sys.path per scripts/, fixtures build/by_name,
schema giocatore) in tests/conftest.py.
"""

import copy
import math

import pytest  # pyright: ignore[reportMissingImports]
from conftest import pid_of
from live_auction import (  # pyright: ignore[reportMissingImports]
    Auction,
    AuctionError,
    InsufficientBudgetError,
    InvalidPriceError,
    InvalidTeamError,
    NotInPoolError,
    SlotUnavailableError,
)

DEFAULT_SLOTS = {"P": 3, "D": 3, "C": 3, "A": 6}


# ---------------------------------------------------------------- vendita valida
def test_vendita_valida_aggiorna_stato(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    assert a.state["money"]["IO"] == 60
    assert a.state["money_league"] == 3 * 100 - 40
    assert a.state["slots"]["IO"]["A"] == 6 - 1
    assert pid_of(a, "ALPHA") not in a.state["pool"]
    assert a.state["sold"] == [(pid_of(a, "ALPHA"), 40, "IO", "A")]
    assert a.state["spent_unknown"] == 0


def test_vendita_al_limite_del_budget(build, by_name):
    a = build(budget=10)
    a.mark_sold(by_name(a, "ALPHA"), 10, "IO")
    assert a.state["money"]["IO"] == 0
    assert pid_of(a, "ALPHA") not in a.state["pool"]


# ------------------------------------------------------------- prezzo non valido
def test_prezzo_non_intero_o_non_positivo(build, by_name):
    a = build()
    for bad in (0, -5, 10.5, True, "10"):
        with pytest.raises(InvalidPriceError):
            a.mark_sold(by_name(a, "ALPHA"), bad, "IO")
        assert pid_of(a, "ALPHA") in a.state["pool"]  # nessuna mutazione
        assert a.state["sold"] == []
        assert a.state["money"]["IO"] == 100


def test_prezzo_oltre_budget(build, by_name):
    a = build(budget=10)
    with pytest.raises(InsufficientBudgetError, match="budget"):
        a.mark_sold(by_name(a, "ALPHA"), 11, "IO")
    assert pid_of(a, "ALPHA") in a.state["pool"]
    assert a.state["money"]["IO"] == 10
    assert a.state["sold"] == []


# ------------------------------------------------- squadra valida oppure ALTRO
def test_squadra_non_tracciata_invalida(build, by_name):
    a = build()
    with pytest.raises(InvalidTeamError, match="ALTRO"):
        a.mark_sold(by_name(a, "ALPHA"), 40, "ROMA")  # pyright: ignore[reportArgumentType]
    assert pid_of(a, "ALPHA") in a.state["pool"]
    assert a.state["spent_unknown"] == 0


def test_altro_esplicito_none_e_stringa(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, None)  # None -> ALTRO
    a.mark_sold(by_name(a, "BETA"), 30, "altro")  # 'ALTRO' case-insensitive
    assert a.state["spent_unknown"] == 70
    assert [s[2] for s in a.state["sold"]] == ["ALTRO", "ALTRO"]
    assert a.state["money"]["IO"] == 100  # squadre tracciate intatte
    assert a.state["slots"]["IO"]["A"] == 6


def test_squadra_normalizzata_case_insensitive(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "io")  # minuscolo -> canonica IO
    assert a.state["money"]["IO"] == 60
    assert a.state["sold"][0][2] == "IO"
    assert a.state["spent_unknown"] == 0


# ------------------------------------------------------------ slot del ruolo
def test_slot_ruolo_esaurito(build, by_name):
    a = build(teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "ALPHA"), 10, "IO")  # A: slot 1 -> 0
    assert a.state["slots"]["IO"]["A"] == 0
    with pytest.raises(SlotUnavailableError, match="slot"):
        a.mark_sold(by_name(a, "BETA"), 10, "IO")  # nessuno slot libero
    assert pid_of(a, "BETA") in a.state["pool"]  # vendita rigettata
    assert a.state["money"]["IO"] == 90  # budget intatto
    assert a.state["sold"] == [(pid_of(a, "ALPHA"), 10, "IO", "A")]


def test_altro_bypassa_limite_slot(build, by_name):
    a = build(teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    a.mark_sold(by_name(a, "ALPHA"), 10, None)  # ALTRO: nessuno slot da scalare
    a.mark_sold(by_name(a, "BETA"), 10, None)
    assert a.state["slots"]["IO"]["A"] == 1  # slot della squadra intatti
    assert a.state["spent_unknown"] == 20


def test_altro_oltre_crediti_della_lega(build, by_name):
    a = build(teams=1, budget=10)
    a.mark_sold(by_name(a, "ALPHA"), 5, None)
    with pytest.raises(InsufficientBudgetError, match="lega"):
        a.mark_sold(by_name(a, "BETA"), 6, None)  # 6 > 5 residui: lega in rosso
    assert pid_of(a, "BETA") in a.state["pool"]
    assert a.state["money_league"] == 5  # mai negativa
    assert a.state["spent_unknown"] == 5


def test_inflazione_mai_negativa_sotto_stato_anomalo(build, by_name):
    a = build(teams=1, budget=10)
    a.mark_sold(by_name(a, "ALPHA"), 5, None)
    a.mark_sold(by_name(a, "BETA"), 5, None)  # lega a zero
    assert a.state["money_league"] == 0
    infl = a.inflation()
    assert infl >= 0 and math.isfinite(infl)
    assert a.inflation_pma() >= 0


def test_altro_esaurisce_la_lega_ma_le_tracciate_restano_valide(build, by_name):
    a = build(teams=1, budget=10)
    a.mark_sold(by_name(a, "ALPHA"), 9, None)  # lega: 10 -> 1
    with pytest.raises(InsufficientBudgetError, match="lega"):
        a.mark_sold(by_name(a, "BETA"), 2, "IO")  # IO ha 10 cr, la lega solo 1
    assert pid_of(a, "BETA") in a.state["pool"]  # nessuna mutazione
    assert a.state["money"]["IO"] == 10
    assert a.state["money_league"] == 1  # mai negativa
    assert a.check_invariants() == []
    a.mark_sold(by_name(a, "BETA"), 1, "IO")  # al limite: 1 cr -> 0, valida
    assert a.state["money_league"] == 0
    assert a.check_invariants() == []


# ------------------------------------------------------------ doppia vendita
def test_doppia_vendita_vietata(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    with pytest.raises(NotInPoolError, match="venduto"):
        a.mark_sold(by_name(a, "ALPHA"), 40, "T1")  # anche da un'altra squadra
    assert len(a.state["sold"]) == 1
    assert a.state["money"]["T1"] == 100


# --------------------------------------------------------- invenduto su venduto
def test_invenduto_solo_se_nel_pool(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    with pytest.raises(NotInPoolError, match="pool"):
        a.mark_unsold(by_name(a, "ALPHA"))
    assert a.state["unsold"].get("alpha", 0) == 0


def test_invenduto_valido_contatore(build, by_name):
    a = build()
    a.mark_unsold(by_name(a, "ALPHA"))
    a.mark_unsold(by_name(a, "ALPHA"))
    assert a.state["unsold"][pid_of(a, "ALPHA")] == 2


def test_vendita_fa_decadere_il_contatore_invenduto(build, by_name):
    # lo sconto invenduto vale solo per il pool: alla vendita il contatore decade
    # (invariante "unsold solo nel pool"), e l'undo lo ripristina dallo snapshot
    a = build()
    a.mark_unsold(by_name(a, "ALPHA"))
    assert a.state["unsold"][pid_of(a, "ALPHA")] == 1
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    assert a.state["unsold"].get(pid_of(a, "ALPHA"), 0) == 0
    assert a.check_invariants() == []
    assert a.undo()
    assert a.state["pool"] and a.state["sold"] == []
    assert a.state["unsold"][pid_of(a, "ALPHA")] == 1  # snapshot coerente
    assert a.check_invariants() == []


# ----------------------------------------------------------------------- undo
def test_undo_vendita_ripristina_stato(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    assert a.undo()
    assert pid_of(a, "ALPHA") in a.state["pool"]
    assert a.state["money"]["IO"] == 100
    assert a.state["slots"]["IO"]["A"] == 6
    assert a.state["money_league"] == 3 * 100
    assert a.state["sold"] == []


def test_undo_invenduto(build, by_name):
    a = build()
    a.mark_unsold(by_name(a, "ALPHA"))
    assert a.state["unsold"][pid_of(a, "ALPHA")] == 1
    assert a.undo()
    assert a.state["unsold"][pid_of(a, "ALPHA")] == 0


def test_vendita_fallita_non_sporca_undo_stack(build, by_name):
    a = build(budget=10)
    with pytest.raises(AuctionError):
        a.mark_sold(by_name(a, "ALPHA"), 11, "IO")
    with pytest.raises(AuctionError):
        a.mark_sold(by_name(a, "ALPHA"), 10, "ROMA")  # pyright: ignore[reportArgumentType]
    assert not a.undo()  # niente snapshot sporchi


# ------------------------------------------- domanda/scarsità patologica
def test_scarsita_finita_e_bounded_normale(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 10, "IO")
    a.mark_sold(by_name(a, "BETA"), 10, "IO")
    sc = a.scarcity(by_name(a, "GAMMA"))
    assert math.isfinite(sc)
    assert a.cfg["scarcity_min"] <= sc <= a.cfg["scarcity_max"]


def test_domanda_negativa_da_vendite_altro(build, by_name):
    # 1 squadra, slot A=1: le vendite ALTRO superano il tetto iniziale
    a = build(teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 1})
    for nome in ("ALPHA", "BETA", "GAMMA"):
        a.mark_sold(by_name(a, nome), 5, None)
    assert a._demand("A") == 0  # mai negativa
    sc = a.scarcity(by_name(a, "DELTA"))
    # NaN non e' finito: isfinite basta a escluderlo (e a escludere +/-inf)
    assert math.isfinite(sc)
    assert a.cfg["scarcity_min"] <= sc <= a.cfg["scarcity_max"]


def test_scarsita_con_demand0_zero(build, by_name):
    a = build(teams=1, slots={"P": 3, "D": 3, "C": 3, "A": 0})
    e = a.evaluate(by_name(a, "ALPHA"))
    assert e["covered"] is True and e["maxbid"] is None  # estremo gestito
    sc = a.scarcity(by_name(a, "ALPHA"))
    assert math.isfinite(sc)
    assert a.cfg["scarcity_min"] <= sc <= a.cfg["scarcity_max"]


def test_pool_vuoto_stato_e_scarsita_sani(build, by_name):
    a = build(teams=1, budget=100, slots={"P": 3, "D": 3, "C": 3, "A": 4})
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        a.mark_sold(by_name(a, nome), 10, "IO")
    assert len(a.state["pool"]) == 0
    assert a.inflation() == 1.0  # valore residuo zero
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        sc = a.scarcity(by_name(a, nome))
        assert math.isfinite(sc)
        assert a.cfg["scarcity_min"] <= sc <= a.cfg["scarcity_max"]
    a.status()  # report non esplode


# ------------------------------------------------- deep-copy dei player
def test_init_non_muta_i_dict_del_chiamante():
    # players completi (schema del listone) costruiti dal chiamante
    def full(nome, pfc):
        return {
            "nome": nome,
            "squadra": "SQ",
            "ruolo": "A",
            "pfc": float(pfc),
            "pma": float(pfc),
            "pfc_range": "",
            "pma_range": "",
            "dpfcpma": 0.0,
            "slot": 2,
            "tit": 60.0,
            "expfm": 6.5,
            "fascia": "Top",
            "status": "T",
            "pen_prob": 0.0,
            "fk_prob": 0.0,
            "tix": 50.0,
            "fix": 50.0,
            "fix_contrib": 0.3,
        }

    caller = [full("ALPHA", 50.0), full("BETA", 40.0)]
    a = Auction(caller, teams=3, budget=100)
    # 'base' viene aggiunto solo ai dict INTERNI, mai a quelli del chiamante
    for p in caller:
        assert "base" not in p
    # il chiamante resta proprietario dei propri dict: mutarlo non tocca l'asta
    caller[0]["pfc"] = 999.0
    assert a.players[pid_of(a, "ALPHA")]["pfc"] == 50.0
    # e viceversa: mutare dentro l'asta non tocca il chiamante
    a.players[pid_of(a, "ALPHA")]["pfc"] = 1.0
    assert caller[0]["pfc"] == 999.0


def test_mark_sold_muta_solo_lo_stato_interno(build, by_name):
    caller_players = []
    a = build()
    for nome in ("ALPHA", "BETA", "GAMMA", "DELTA"):
        p = by_name(a, nome)
        caller_players.append(
            {
                k: v for k, v in p.items() if k not in ("base", "pid")
            }  # snapshot del chiamante
        )
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    # i dati del chiamante (senza 'base'/'pid' aggiunti dal motore) sono invariati
    for orig, nome in zip(
        caller_players, ("ALPHA", "BETA", "GAMMA", "DELTA"), strict=True
    ):
        current = by_name(a, nome)
        for k, v in orig.items():
            assert current[k] == v, f"{nome}.{k} mutato"
    assert a.state["sold"] == [(pid_of(a, "ALPHA"), 40, "IO", "A")]


def norm_key(nome):
    return nome.lower().strip()


# ------------------------------------------------------- check_invariants
def test_invarianti_stato_iniziale(build):
    for kwargs in (
        {},
        {"teams": 1},
        {"teams": 4, "budget": 50},
        {"teams": 2, "budget": 200, "slots": {"P": 2, "D": 4, "C": 4, "A": 4}},
    ):
        a = build(**kwargs)
        assert a.check_invariants() == []


def test_invarianti_dopo_vendite_miste(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")  # tracciata
    a.mark_sold(by_name(a, "BETA"), 30, None)  # ALTRO
    a.mark_unsold(by_name(a, "GAMMA"))
    assert a.check_invariants() == []
    # ledger ALTRO: money_league == sum(money) - spent_unknown == total0 - somma
    assert (
        a.state["money_league"]
        == sum(a.state["money"].values()) - a.state["spent_unknown"]
    )
    assert a.state["money_league"] == a.total_money0 - sum(
        p for _, p, _, _ in a.state["sold"]
    )


def test_invarianti_dopo_altro_esaurisce_la_lega(build, by_name):
    a = build(teams=1, budget=10)
    a.mark_sold(by_name(a, "ALPHA"), 5, None)
    a.mark_sold(by_name(a, "BETA"), 5, None)  # lega a zero
    assert a.state["money_league"] == 0
    assert a.check_invariants() == []


def test_invarianti_dopo_undo_su_tutte_le_operazioni(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.mark_sold(by_name(a, "BETA"), 30, None)
    a.mark_unsold(by_name(a, "GAMMA"))
    steps = 0
    while a.undo():
        assert a.check_invariants() == []  # invariante 5: valide dopo ogni undo
        steps += 1
    assert steps == 3
    assert a.check_invariants() == []


def test_check_invariants_non_muta_lo_stato(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.mark_unsold(by_name(a, "BETA"))
    before = copy.deepcopy(a.state)
    out1 = a.check_invariants()
    out2 = a.check_invariants()
    assert out1 == [] and out2 == out1
    assert a.state == before  # nessuna mutazione, nemmeno su defaultdict


# --------------------------------------- corruzioni che gli invarianti rilevano
def test_invariante_rileva_ledger_corrotto(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["money_league"] = 999
    found = a.check_invariants()
    assert found
    assert any("ledger" in p for p in found)


def test_invariante_rileva_ledger_spent_unknown_corrotto(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 30, None)  # ALTRO
    a.state["spent_unknown"] = 0  # somma vendite ALTRO non piu' contabilizzata
    found = a.check_invariants()
    assert any("spent_unknown" in p for p in found)
    assert any("ledger" in p for p in found)


def test_invariante_rileva_budget_negativo(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["money"]["IO"] = -1
    found = a.check_invariants()
    assert any("budget" in p for p in found)


def test_invariante_rileva_pool_con_chiave_estranea(build):
    a = build()
    a.state["pool"].add("fantasma")
    found = a.check_invariants()
    assert any("fantasma" in p for p in found)


def test_invariante_rileva_vendita_duplicata(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["sold"].append(a.state["sold"][0])  # doppione
    found = a.check_invariants()
    assert any("duplicate" in p for p in found)


def test_invariante_rileva_venduto_ancora_nel_pool(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["pool"].add(pid_of(a, "ALPHA"))  # riappare nel pool (doppio stato)
    found = a.check_invariants()
    assert any("pool" in p for p in found)


def test_invariante_rileva_giocatore_sparito(build, by_name):
    a = build()
    pid = pid_of(a, "BETA")
    a.state["pool"].discard(pid)  # ne' in pool ne' venduto
    found = a.check_invariants()
    assert any(pid in p for p in found)


def test_invariante_rileva_slot_negativo(build, by_name):
    a = build()
    a.state["slots"]["IO"]["A"] = -1
    found = a.check_invariants()
    assert any("negativo" in p for p in found)


def test_invariante_rileva_slot_non_coerente_con_vendite(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")  # A: 6 -> 5
    a.state["slots"]["IO"]["A"] = 6  # corruzione: non ha scalato
    found = a.check_invariants()
    assert any("IO/A" in p and "iniziali" in p for p in found)


def test_invariante_rileva_unsold_su_venduto(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["unsold"][pid_of(a, "ALPHA")] = (
        1  # invenduto su giocatore non piu' nel pool
    )
    found = a.check_invariants()
    assert any("unsold" in p and "pool" in p for p in found)


def test_invariante_rileva_vendita_a_squadra_sconosciuta(build, by_name):
    a = build()
    a.mark_sold(by_name(a, "ALPHA"), 40, "IO")
    a.state["sold"].append(("beta", 5, "ROMA", "A"))
    found = a.check_invariants()
    assert any("ROMA" in p for p in found)


def test_invariante_rileva_chiavi_di_stato_inattese(build):
    a = build()
    a.state["extra"] = 1
    found = a.check_invariants()
    assert any("chiavi inattese" in p for p in found)
