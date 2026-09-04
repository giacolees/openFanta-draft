"""openFanta-draft — strumenti per l'asta del Fantacalcio (Listone Fantaculo).

Sottopacchetti:

- :mod:`openfanta.core` — motore dell'asta e dominio (config, valutazione,
  calibrazione, gate, mantra, store event-sourced);
- :mod:`openfanta.forward` — simulatore forward (stato, bidding, sim, agg, CLI);
- :mod:`openfanta.ingest` — import del listone e dei ruoli mantra;
- :mod:`openfanta.backtest` — backtest prezzo (replay) e rendimento;
- :mod:`openfanta.web` — GUI FastAPI dell'asta live.
"""

__version__ = "0.1.0"
