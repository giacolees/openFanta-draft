"""Infrastruttura di test condivisa (WP1).

- Inserisce ``scripts/`` in ``sys.path`` una sola volta, prima di ogni test
  module: i test importano ``live_auction`` / ``web_auction`` senza duplicare
  il path in testa a ogni file.
- Fixtures essenziali del dominio: ``build`` (fabbrica di ``Auction`` di test),
  ``by_name`` (lookup sicuro con assert) e gli helper di costruzione giocatori
  (``make_player`` / ``PLAYERS``), unica fonte dello schema giocatore nei test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # pyright: ignore[reportMissingImports]
from live_auction import Auction  # pyright: ignore[reportMissingImports]

DEFAULT_SLOTS = {"P": 3, "D": 3, "C": 3, "A": 6}
# Formazione di riferimento coerente con DEFAULT_SLOTS: la fabbrica deriva
# sempre una formation <= slots effettivi (invariante strutturale di
# league_config, valida anche nel profilo "engine" del costruttore).
DEFAULT_FORMATION = {"P": 1, "D": 1, "C": 1, "A": 4}


def make_player(
    nome, ruolo="A", pfc=50.0, slot=2, tit=60.0, fix_contrib=0.3, squadra="", status="T"
):
    """Giocatore nel formato del listone: lo schema canonico dei test."""
    return {
        "nome": nome,
        "squadra": squadra,
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
        "status": status,
        "pen_prob": 0.0,
        "fk_prob": 0.0,
        "tix": 50.0,
        "fix": 50.0,
        "fix_contrib": float(fix_contrib),
    }


PLAYERS = [
    make_player("ALPHA"),
    make_player("BETA"),
    make_player("GAMMA"),
    make_player("DELTA"),
]


@pytest.fixture
def build():
    """Fabbrica di Auction di test: 3 squadre (IO, T1, T2) da 100 cr, ruolo A 6 slot.

    I player di default vengono copiati per test: nessuno stato condiviso tra
    test, e il costruttore (deep-copy) resta indipendente dal chiamante.
    """

    def _build(players=None, **overrides):
        kwargs = {"teams": 3, "budget": 100, "slots": dict(DEFAULT_SLOTS)}
        kwargs.update(overrides)
        slots = kwargs["slots"]
        # formation coerente con gli slot della singola build (mai > slots)
        kwargs["formation"] = {
            r: min(DEFAULT_FORMATION[r], slots[r]) for r in DEFAULT_FORMATION
        }
        return Auction(players or [dict(q) for q in PLAYERS], **kwargs)

    return _build


@pytest.fixture
def by_name():
    """Lookup sicuro di un giocatore: fallisce con assert se il nome non esiste."""

    def _by_name(auction, nome):
        p = auction.find(nome)
        assert p is not None, f"giocatore {nome!r} non trovato"
        return p

    return _by_name


def pid_of(auction, nome):
    """pid del giocatore trovato per nome (WP3: il motore identifica con il pid,
    i test dello stato usano pid_of()* / le chiavi di pool, sold e unsold)."""
    p = auction.find(nome)
    assert p is not None, f"giocatore {nome!r} non trovato"
    return p["pid"]
