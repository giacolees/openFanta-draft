"""Motore dell'asta e dominio condiviso.

- :mod:`openfanta.core.auction` — motore live (``Auction``) + CLI;
- :mod:`openfanta.core.config` — unico schema/validatore della lega;
- :mod:`openfanta.core.valuation` — valutazione per-singola-squadra e maxbid;
- :mod:`openfanta.core.calibration` — calibrazione advisory dai prezzi reali;
- :mod:`openfanta.core.gates` — gate di qualita' (soglie e verdict puri);
- :mod:`openfanta.core.mantra` — ruoli/formazioni mantra;
- :mod:`openfanta.core.store` — persistenza SQLite event-sourced.
"""
