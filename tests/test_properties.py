"""Test di proprietà su sequenze casuali (seedate) di operazioni d'asta (WP1).

Generatore deterministico di operazioni VALIDE su configurazioni casuali:
vendite a squadre tracciate, vendite ALTRO, invenduti e undo, intervallati
a caso. Dopo OGNI operazione si verifica ``check_invariants() == []`` e a
fine sequenza si fa undo fino allo svuotamento (lo stato deve tornare
identico a quello iniziale, invarianti valide a ogni passo).

Nessuna dipendenza nuova: solo ``random.Random`` con seed fisso.
"""

import copy
import random

import pytest  # pyright: ignore[reportMissingImports]

from openfanta.core.auction import Auction  # pyright: ignore[reportMissingImports]

ROLE_ORDER = ["P", "D", "C", "A"]


def make_player(nome, ruolo, pfc=30.0, slot=3):
    """Giocatore compilato nel formato del listone con slot 0..4."""
    return {
        "nome": nome,
        "squadra": f"SQ{ruolo}",
        "ruolo": ruolo,
        "pfc": float(pfc),
        "pma": float(pfc) * 0.9,
        "pfc_range": "",
        "pma_range": "",
        "dpfcpma": 0.0,
        "slot": slot,
        "tit": 50.0,
        "expfm": 6.5,
        "fascia": "Top",
        "status": "T",
        "pen_prob": 0.0,
        "fk_prob": 0.0,
        "tix": 50.0,
        "fix": 50.0,
        "fix_contrib": 0.3,
    }


def random_config(rng):
    """Configurazione casuale: teams 1-4, budget 10-300, slot per ruolo 0-4.
    La formation e' sempre <= slot (invariante strutturale di league_config,
    valida anche nel profilo engine del costruttore)."""
    slots = {role: rng.randint(0, 4) for role in ROLE_ORDER}
    formation = {role: rng.randint(0, slots[role]) for role in ROLE_ORDER}
    return {
        "teams": rng.randint(1, 4),
        "budget": rng.randint(10, 300),
        "slots": slots,
        "formation": formation,
    }


def run_sequence(rng, cfg, force_altro_only=False, max_steps=200):
    """Esegue una sequenza casuale di operazioni valide e verifica gli invarianti.

    Dopo ogni operazione (e dopo ogni undo) `check_invariants()` deve essere
    vuoto; a fine sequenza si annulla tutto fino allo stato iniziale, uguale a
    quello di un Auction fresco con la stessa configurazione.
    """
    pool_size = 6 + rng.randint(0, 4)  # 6-10 giocatori per ruolo
    players = []
    for role in ROLE_ORDER:
        for i in range(1, pool_size + 1):
            players.append(make_player(f"{role}{i:02d}", role, slot=rng.randint(0, 4)))
    a = Auction([dict(q) for q in players], **cfg)
    initial_state = copy.deepcopy(a.state)
    assert a.check_invariants() == []

    counts = {"tracked": 0, "altro": 0, "unsold": 0, "undo": 0}
    for _ in range(max_steps):
        state, money = a.state, a.state["money"]
        moves = []

        # vendita a squadra tracciata: prezzo valido <= budget, slot libero,
        # e anche <= crediti residui della lega (il totale non va in rosso)
        if not force_altro_only and any(
            state["money_league"] >= 1
            and money[t] >= 1
            and state["slots"][t][a.players[k]["ruolo"]] >= 1
            for k in state["pool"]
            for t in money
        ):
            moves.append("tracked")
        # vendita ALTRO: prezzo valido <= crediti residui della lega
        if state["money_league"] >= 1 and state["pool"]:
            moves.append("altro")
        # invenduto: sempre possibile finche' il pool non e' vuoto
        if state["pool"]:
            moves.append("unsold")
        # undo: possibile se ci sono snapshot
        if a.undo_stack:
            moves.append("undo")

        if not moves:
            break
        kind = moves[rng.randrange(len(moves))]

        if kind == "tracked":
            valid = [
                (key, team)
                for key in state["pool"]
                for team in money
                if state["money_league"] >= 1
                and money[team] >= 1
                and state["slots"][team][a.players[key]["ruolo"]] >= 1
            ]
            key, team = valid[rng.randrange(len(valid))]
            price = rng.randint(1, min(money[team], state["money_league"], 25))
            a.mark_sold(a.players[key], price, team)
            counts["tracked"] += 1
        elif kind == "altro":
            key = rng.choice(sorted(state["pool"]))
            price = rng.randint(1, min(state["money_league"], 25))
            a.mark_sold(a.players[key], price, None)
            counts["altro"] += 1
        elif kind == "unsold":
            key = rng.choice(sorted(state["pool"]))
            a.mark_unsold(a.players[key])
            counts["unsold"] += 1
        elif kind == "undo":
            assert a.undo()
            counts["undo"] += 1

        violations = a.check_invariants()
        assert violations == [], (
            f"invarianti violate dopo '{kind}' (seed {cfg}): {violations}"
        )

    # annulla tutto fino allo stato iniziale: invarianti valide a ogni passo
    while a.undo():
        violations = a.check_invariants()
        assert violations == [], f"invarianti violate dopo undo: {violations}"
        counts["undo"] += 1

    assert a.check_invariants() == []
    # lo stato svuotato coincide con lo stato iniziale (e con un Auction fresco)
    assert a.state == initial_state
    fresh = Auction([dict(q) for q in players], **cfg)
    assert a.state == fresh.state

    return counts


SEEDS = [0, 1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("seed", SEEDS)
def test_sequenze_casuali_miste_rispettano_gli_invarianti(seed):
    rng = random.Random(seed)
    cfg = random_config(rng)
    counts = run_sequence(rng, cfg)
    # sequenza non degenere: deve essere successo qualcosa di venduto/annullato
    assert counts["tracked"] + counts["altro"] >= 1


@pytest.mark.parametrize("seed", SEEDS)
def test_sequenze_di_solo_vendite_altro_rispettano_il_ledger(seed):
    """Solo vendite ALTRO (piu' invenduti/undo): il ledger con spent_unknown
    resta coerente (money_league == sum(money) - spent_unknown)."""
    rng = random.Random(1000 + seed)
    cfg = random_config(rng)
    counts = run_sequence(rng, cfg, force_altro_only=True)
    assert counts["altro"] >= 1
    assert counts["tracked"] == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_sequenze_su_configurazioni_estreme(seed):
    """Configurazioni ai bordi: budget minimi e slot tutti zero (solo ALTRO/
    invenduti/undo), squadra singola."""
    rng = random.Random(2000 + seed)
    zero = {"P": 0, "D": 0, "C": 0, "A": 0}
    cfg = {
        "teams": 1,
        "budget": 10,
        "slots": dict(zero),
        "formation": dict(zero),  # formation == slot: invariante valida anche qui
    }
    counts = run_sequence(rng, cfg)
    assert counts["tracked"] == 0  # nessuno slot libero: mai vendite tracciate
