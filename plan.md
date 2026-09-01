# Piano di implementazione — openFanta-draft (roadmap 2026/27)

Piano pointer-based per implementare TUTTA la roadmap concordata. Ogni WP è
assegnabile a **un agente** e ordinato per dipendenze. Nessuna stima temporale,
nessun redesign estetico. **Non fare commit** (repo senza commit iniziali:
tutto è untracked).

> ⚠️ **Stato del repo**: file in evoluzione simultanea (i test sono passati da
> 19 a 21 unit durante la stesura del piano; `live_auction.py` è cresciuto).
> I numeri di riga indicati sono un riferimento al momento della stesura:
> ogni agente deve **rileggere la funzione puntata prima di modificare**.
> La sequenza dei WP è lineare proprio per evitare che due agenti tocchino lo
> stesso file in parallelo: rispettare la colonna "ownership" di ogni WP.

---

## 1. Stato attuale verificato

### Pipeline asta (dominio)

| File | Ruolo | Punti chiave |
| --- | --- | --- |
| `data/Listone_Fantaculo_*.xlsx` | input giornaliero | nome file con data |
| `scripts/import_listone.py` | xlsx → `data/listone.csv` | `CANON` (riga 41), `COLUMN_MAP` (riga 46), `import_listone()` (riga 104), `latest_file()` (riga 56) |
| `data/listone.csv` | libro canonico giocatori | identità = `nome` normalizzato; **nessun ID stabile, nessun metadato stagione** |
| `scripts/live_auction.py` | motore dominio | `DEFAULTS` (riga 69), `load_players()` (riga 113), `Auction` (riga 181): `__init__` (182), `_init_state` (190), `_snapshot` (234), `scarcity` (302), `quality_left` (329), `evaluate` (375), `_validate_sale` (429), `mark_sold` (460), `mark_unsold` (474), `undo` (484), `status` (491), `format_eval` (544), `CONFIG_KEYS` (620), `run_repl` (636) |
| `scripts/web_auction.py` | GUI FastAPI | `TrendAuction` (riga 47): `__init__` (52), `_record` (56), `mark_sold` (67), `undo` (75), `trend` (102); `player_payload` (157), `api_state` (183), `ConfigBody` (203), `api_config`/`api_config_post` (211/217), `api_sold` (267), `api_undo` (292), `api_trend` (297), `csv_response` (306), `main` (376) |
| `scripts/static/index.html` | SPA vanilla JS | render **solo via `textContent`** (sicuro XSS), helper `el()` (riga ~615); config dialog, export CSV, trend chart |

### Pipeline statistica (evidenza, non usata nell'asta)

| File | Ruolo |
| --- | --- |
| `scripts/scrape_serie_a_stats_2025_26.py` | scrape stats → `data/serie_a_stats_*/giocatori_*.csv` |
| `scripts/build_stats_rating_2025_26.py` | ridge per ruolo (pure Python), LOOCV, rating 0-100 (`fit_role_model` riga 500, `compute_ratings` riga 591) |
| `scripts/test_forward_2024_25.py` | forward test 24/25 → FM 25/26 (`main` riga 95) |
| `scripts/compare_models_forward.py` | confronto modelli (`main` riga 250) — **importa numpy (riga 30) senza dichiararlo** |
| `scripts/build_player_ratings.py` | legacy, fuori pipeline (README) |

### Test e dipendenze

- `tests/test_live_auction.py`: 21 test verdi (`uv run pytest -q` → `21 passed`).
- **Gap**: `pytest` è in `.venv` ma **non dichiarato** in `pyproject.toml` (righe 3-10: solo fastapi, openpyxl, uvicorn). `numpy` è importato da `compare_models_forward.py` ma **assente**: `uv run python -c "import numpy"` → `ModuleNotFoundError` (verificato). `httpx` assente (serve a `fastapi.testclient.TestClient`). Shebang misti: alcuni script usano `#!/usr/bin/env python3.10` (non portabile), altri `#!/usr/bin/env -S uv run --quiet python`.
- `.python-version` = 3.10; `requires-python >= 3.10`.

### Invarianti oggi garantite (da NON regredire)

1. Prezzo intero positivo, squadra tracciata o `ALTRO` esplicito, budget non negativo (anche per `ALTRO` vs crediti lega: `_validate_sale` riga 429).
2. Slot mai negativi; nessuna doppia vendita; `invenduto` solo su giocatori in pool.
3. `undo` = ripristino snapshot (stack max 100, `_snapshot` riga 234) — solo in memoria.
4. `inflation`/`inflation_pma` mai negative e finite; `scarcity` bounded in `[scarcity_min, scarcity_max]`; `_demand` mai negativo.
5. Vendita fallita ⇒ nessuna mutazione né snapshot sporco (testato).
6. Config lega solo `teams/budget/names/io` via web (`ConfigBody` riga 203); la CLI ha più chiavi (`CONFIG_KEYS` riga 620: slots, formation, soglia-tit) → **incoerenza API/CLI** da sanare.

---

## 2. Principi trasversali

- **Pointer-based**: ogni WP cita file:funzione e rilegge il codice prima di editare.
- **Sequenzialità per file**: `live_auction.py` è toccato da WP1, WP2, WP3 (load_players), WP5, WP6, WP7 → **mai due WP in parallelo sullo stesso file**. `web_auction.py` da WP2, WP4, WP8, WP10. `index.html` solo da WP10.
- **Contratto di lavoro agenti**: claim del WP → implementazione → acceptance del WP → `lsp_diagnostics` + `lens_diagnostics mode=all` prima di chiudere. Mai editare `plan.md`/`.agent/`; segnalare drift nel summary.
- **Decisioni dure → `.agent/decisions.md`** (ADR) nel WP che le prende (WP9 in particolare). Termini di dominio → `.agent/domain.md`.
- **Test come parte del WP**: ogni WP consegna i propri test; WP11 consolida e chiude il cerchio E2E.
- **Niente promozione di evidenza debole**: il modello statistico FM (`build_stats_rating`) **non entra** nel prezzo d'asta; vedi gate WP9.

---

## 3. Architettura target (delta rispetto a oggi)

```
                 data/listone_meta.json        (stagione, fonte, data, tot/ruolo)
                 data/import_report_*.json     (errori/warning dell'import)
                                                      │
data/Listone_*.xlsx → import_listone.py → data/listone.csv  (+pid, +range numerici, +incertezza)
                                                      │ load_players()
scripts/league_config.py  (schema config unico: validate/normalize/feasibility)
scripts/valuation.py      (market | fantasy | my_team: scarcity_team, maxbid breakdown)
scripts/calibration.py    (cell role×band×fase, robusta, shrinkage, confidenza)
scripts/auction_store.py  (SQLite event-sourced: resume, rettifica, replay, backup/import)
scripts/auction.py        ── live_auction.py (Auction puro, invarianti, no I/O)
scripts/web_auction.py    (FastAPI: wrapping + endpoint nuovi + persistenza attiva)
scripts/backtest_auction.py | scripts/backtest_yield.py | scripts/gates.py
scripts/static/index.html (blocchi mercato/fantasy/rosa, calibrazione, persistenza, feasibility)
```

Nuovi file: `scripts/league_config.py`, `scripts/valuation.py`, `scripts/calibration.py`,
`scripts/auction_store.py`, `scripts/backtest_auction.py`, `scripts/backtest_yield.py`,
`scripts/gates.py`, `tests/conftest.py`, `tests/test_properties.py`, `tests/test_config.py`,
`tests/test_import.py`, `tests/test_auction_store.py`, `tests/test_valuation.py`,
`tests/test_calibration.py`, `tests/test_web_api.py`, `tests/test_acceptance.py`.

---

## 4. Work package (ordinati per dipendenze)

Mappa item roadmap → WP:

| # WP | Item roadmap | Titolo |
| --- | --- | --- |
| WP1 | 1 | Correttezza dominio e invarianti |
| WP2 | 2 | API/configurazione coerente e fattibilità lega |
| WP3 | 4 | ID stabile, schema import validato, metadati, error report, range/incertezza |
| WP4 | 3 | Persistenza SQLite event-sourced |
| WP5 | 7 | Scarsità quantitativa/qualitativa/economica per slot e budget delle singole squadre |
| WP6 | 6 | Max bid: costo opportunità, riserva completamento rosa, allocazioni ruolo, alternative |
| WP7 | 5 | Separazione esplicita prezzo mercato / valore fantacalcistico / valore per la mia rosa |
| WP8 | 8 | Calibrazione adattiva dalle vendite |
| WP9 | 9 | Backtest distinti prezzo d'asta e rendimento + gate qualità |
| WP10 | 10 | Frontend operativo |
| WP11 | 11 | Test unitari, API, persistenza, replay, proprietà/invarianti, acceptance E2E |
| WP12 | 12 | Hardening minimo (XSS, concorrenza scritture, portabilità) |

Ordine per dipendenze: WP1 → WP2 → WP3 → WP4 → WP5 → WP6 → WP7 → WP8 → WP9 → WP10 → WP11 → WP12.

---

### WP1 — Correttezza dominio e invarianti (roadmap #1)

**Scope**

- Dichiarare l'infrastruttura test minima mancante (prerequisito trasversale).
- Rendere il dominio robusto e **verificabile**: deep-copy dei player, validator
  di invarianti ispezionabile, test di proprietà su sequenze casuali di operazioni.

**File previsti**

- `pyproject.toml`: aggiungere `[dependency-groups] dev = ["pytest>=8", "httpx>=0.27"]`
  (httpx serve al TestClient già qui o al più tardi in WP11) e `numpy` in `dependencies`
  (è dipendenza già usata da `scripts/compare_models_forward.py:30`, oggi non dichiarata).
- `tests/conftest.py` (nuovo): centralizzare l'aggiunta di `scripts/` a `sys.path`
  (oggi fatto in testa a `tests/test_live_auction.py:14`).
- `scripts/live_auction.py`:
  - `Auction.__init__` (riga 182): **deep-copy** ogni dict player (`copy.deepcopy` del
    dizionario, oggi `players` viene riusato e mutato con `p["base"]` in `_init_state` riga 190).
  - Nuovo `Auction.check_invariants() -> list[str]`: ritorna le violazioni (lista vuota = ok):
    1. **Ledger monetario**: `money_league == teams*budget − Σ(prezzi vendite)` e
       `sum(money.values()) + spent_unknown == money_league`; mai negativi.
    2. **Pool**: `pool ⊆ players`; chiavi in `sold` tutte in `players` e **non** in `pool`;
       nessun duplicato in `sold`; per (team,ruolo) tracciati:
       `slot_attuali == slot_iniziali − n_vendite_non_ALTRO`.
    3. **Unsold** solo per chiavi in `pool`.
    4. Tutti i valori numerici di stato finiti (`math.isfinite`) e slot ≥ 0.
    5. Dopo `_snapshot`/`undo`, le invarianti 1-4 continuano a valere.
- `tests/test_properties.py` (nuovo): generatore seeded di sequenze casuali di
  operazioni valide (`mark_sold` con prezzo valido, `mark_unsold`, `undo`, vendite
  `ALTRO`) su configurazioni casuali (teams 1-4, budget, slot); dopo ogni operazione
  assert `check_invariants() == []`; concludere sempre con `undo` fino a vuoto e
  ri-controllare. Nessuna nuova dipendenza (RNG di `random` con seed fisso).
- `tests/test_live_auction.py`: invarianti 1-4 come test espliciti (alcuni già coperti).

**Dipendenze** — nessuna (prerequisito).

**Invarianti/decisioni da mantenere** — tutte le 6 invarianti dello stato attuale
(sezione 1) più il ledger 1-5 sopra. `undo` resta snapshot-based in memoria in questo WP.

**Scelta conservativa (default)** — `numpy` va in `dependencies` (non in extra):
è già un runtime dep de facto di `compare_models_forward.py`; dichiararlo evita il
`ModuleNotFoundError` verificato. Alternativa (extra `stats = ["numpy"]`) solo se si
vuole tenere l'ambiente asta senza bloat: default = dipendenza normale.

**Acceptance commands**

```
uv sync && uv run pytest -q          # verde, ≥ 21 test esistenti + nuovi
uv run pytest tests/test_properties.py -q
```

**Criteri binari** — `check_invariants()` esiste e passa su tutte le suite;
`Auction.__init__` non condivide più i dict del chiamante (test di mutazione);
`uv run pytest -q` verde.

---

### WP2 — API/configurazione coerente e fattibilità lega (roadmap #2)

**Scope**

- Un unico schema di configurazione lega condiviso da CLI e web.
- Validazione strutturale + **fattibilità**: il budget della lega deve poter coprire
  il completamento minimo della rosa; slot ≥ formazione; nomi unici; io presente.

**File previsti**

- `scripts/league_config.py` (nuovo): `normalize(cfg)`, `validate(cfg) -> list[str]`
  (errori strutturali), `feasibility(cfg, players) -> list[str]` (warning/errori di
  fattibilità), `minimum_roster_cost(cfg) -> float` (= Σ per ruolo di `min(slots[r], n_giocatori_disponibili_r)` × floor ruolo; floor condiviso con WP6).
- `scripts/live_auction.py`:
  - `Auction.__init__` (riga 182): chiama `league_config.validate` e alza `ValueError`
    con messaggio se ci sono errori (senza rompere i test esistenti: le config di test
    sono già valide).
  - `run_repl` → comando `config` (riga ~690): validare PRIMA di azzerare; su errore
    stampare la lista errori e **non** resettare (oggi resetta comunque).
  - `CONFIG_KEYS` (riga 620): riusare le stesse chiavi normalizzate di league_config.
- `scripts/web_auction.py`:
  - `ConfigBody` (riga 203): aggiungere `slots?`, `formation?`, `soglia_tit?`, `team_names?`
    per allineare ai comandi CLI; validare via `league_config` (nessuna logica ad hoc doppia).
  - `api_config_post` (riga 217): risposta include `feasibility` (errori+warnings) e costi
    minimi; `400` con messaggio su errori strutturali.
  - `api_config` (riga 211): espone l'intero config normalizzato.
- `README.md` (sezione Config): aggiornare.

**Dipendenze** — WP1 (invarianti su cui poggia la validazione).

**Invarianti/decisioni da mantenere** — range attuali (2-20 squadre, 10-10000 crediti,
nomi unici, io ∈ nomi); CLI e web **stesso** validatore; nessuna configurazione
invalida può azzerare l'asta.

**Scelta conservativa (default)** — errori strutturali = bloccare (hart error);
warning di fattibilità (es. budget rigido ma raggiungibile) = non bloccanti ma esposti
nella risposta e (WP10) in un banner. `minimum_roster_cost` usa il floor piatto
corrente `min_slot_price=2` finché WP6 introduce i floor per ruolo.

**Acceptance commands**

```
uv run pytest tests/test_config.py tests/test_live_auction.py -q
uv run python scripts/live_auction.py --demo     # uscita invariata, exit 0
uv run python - <<'EOF'
from scripts.league_config import validate, feasibility
print(validate({"teams":8,"budget":10,"slots":{"P":3,"D":8,"C":8,"A":6},"formation":{"P":1,"D":4,"C":4,"A":2}}))
EOF
```

**Criteri binari** — config invalida → lista errori (CLI) o `400` (web) senza reset;
risposta config contiene `feasibility`; `--demo` identico; suite verde.

---

### WP3 — ID stabile, schema import validato, metadati stagione, error report, range/incertezza (roadmap #4)

**Scope**

- Identità **stabile** dei giocatori attraverso i refresh giornalieri del listone.
- Import che **fallisce forte** su errori strutturali e **riporta** errori/warning.
- Metadati stagione/fonte/data in sidecar; range di prezzo resi numerici + incertezza.

**File previsti**

- `scripts/import_listone.py`:
  - Nuova colonna canonica `pid` in `CANON` (riga 41): `fnv1a64(norm(nome) + "|" + norm(squadra) + "|" + ruolo)`
    esadecimale, **deterministico** e stabile finché nome/squadra/ruolo non cambiano.
  - Colonne nuove: `pfc_lo, pfc_hi, pma_lo, pma_hi` (numeri da `pfc_range`/`pma_range`,
    già in input) e `unc_pfc = round((hi−lo)/2,1)` (incertezza = semiampiezza).
  - Validazione schema in `import_listone` (riga 104): colonne obbligatorie presenti
    (mappa `COLUMN_MAP` riga 46), tipi numerici, range (`pfc>0`, `pma>0`, `slot 1..8`,
    `tit 0..100`, `expfm 0..10`), duplicati su `pid`/`nome+squadra+ruolo`.
  - Error report: `data/import_report_<ts>.json` con `{errors, warnings, n_imported,
    n_skipped, pid_collisions}`; exit code **2** se errori bloccanti (manca colonna,
    pid duplicati, pfc non numerico), altrimenti 0 con warning.
  - Metadati: scrivere `data/listone_meta.json` `{season:"2026-27", source_file,
    imported_at, n_players, n_by_role, total_pfc_by_role, columns, warnings}`.
- `scripts/live_auction.py` → `load_players` (riga 113): leggere `pid`, `pfc_lo/hi`,
  `pma_lo/hi`, `unc_pfc` con default **backward-compatible** (assenti → `pid=norm(nome)`,
  range numerici derivati dal range stringa se possibile, altrimenti `None`).
- `tests/test_import.py` (nuovo): workbook sintetico openpyxl in `tmp_path`:
  (a) happy path → colonne nuove+pids stabili al re-import; (b) colonna mancante →
  errore bloccante + report; (c) pid duplicato → errore bloccante; (d) valori sporchi
  → warning senza crash; (e) ri-import dello stesso xlsx → pids identici (stabilità).

**Dipendenze** — WP2 (per l'ordine dei file: `load_players` sta in `live_auction.py`
che WP2 ha appena toccato). Indipendente da WP4 (possono chiamarsi "da fare in serie").

**Invarianti/decisioni da mantenere** — il CSV canonico resta compatibile in lettura
con `load_players` attuale; nuovi campi solo aggiuntivi; il ri-import giornaliero non
cambia i `pid` dei giocatori in carta (base della persistenza WP4).

**Scelta conservativa (default)** — identità = `norm(nome)|squadra|ruolo` hashato:
nessuna fonte esterna, deterministico, stabile al refresh. Su collisione di `pid` →
errore bloccante con lista nel report (mai indovinare/unire automaticamente).

**Acceptance commands**

```
uv run python scripts/import_listone.py              # regenera listone.csv + meta, exit 0
uv run python scripts/import_listone.py --file data/Listone_Fantaculo_2026_09_01.xlsx
uv run pytest tests/test_import.py tests/test_live_auction.py -q
```

**Criteri binari** — `listone.csv` contiene `pid,pfc_lo,pfc_hi,pma_lo,pma_hi,unc_pfc`;
`listone_meta.json` esiste con stagione/fonte/data; suite verde; exit code 2 su file
invalido sintetico.

---

### WP4 — Persistenza SQLite event-sourced (roadmap #3)

**Scope**

- L'asta diventa **ripristinabile**: log eventi in SQLite, replay deterministico,
  rettifica eventi (revoke/restate), backup/import, resume alla ripartenza.

**File previsti**

- `scripts/auction_store.py` (nuovo):
  - SQLite `sqlite3`, `PRAGMA journal_mode=WAL`; tabella `events(id INTEGER PRIMARY KEY,
    seq INTEGER UNIQUE, ts TEXT, type TEXT, payload TEXT NOT NULL /*JSON*/,
    supersedes INTEGER NULL)`; tabella `meta(key TEXT PRIMARY KEY, value TEXT)`
    con `schema_version` e `league_cfg` (snapshot config per replay).
  - API: `append(type, payload, supersedes=None)`, `read_all()`, `backup(path)`
    (dump JSON dei soli eventi), `import_events(path, mode="append"|"replace")`
    (validazione struttura + `supersedes` riferiti), `close()`.
  - **Scrittura**: `threading.Lock` in-process + `BEGIN IMMEDIATE` (single writer);
    `seq` monotono via `INSERT ... RETURNING` (SQLite ≥3.35) o max+1 in transazione.
  - `apply_event(auction, ev) -> None`: dispatcher deterministico
    (`league_configured`, `sold`, `unsold`, `revoke`, `restate`) che ri-issua i
    comandi sul motore; dopo ogni evento → `check_invariants()` (WP1).
  - Eventi con payload: `{pid, nome, price, team, ruolo, ...}` (pid da WP3).
- `scripts/web_auction.py`:
  - `main` (riga 376): `--store PATH` (default `data/asta_stagione_2026_27.db`);
    all'avvio costruisce la `TrendAuction` **replay del log** (resume); il config
    iniziale diventa il primo evento `league_configured`.
  - Le mutazioni (`api_sold` 267, `api_unsold` 283) appendono l'evento prima di
    rispondere; `TrendAuction.events` (riga 56) viene **derivato** dal log (unica
    fonte di verità, niente doppio stato divergente).
  - `api_undo` (riga 292) in modalità persistita → **rettifica**: appende `revoke`
    (annulla l'ultima vendita: rimborso team+lega, slot++ , pool+) oppure `unsold_revoke`;
    niente pop silenzioso. La modalità in-memory (CLI/`--demo`) conserva l'`undo_stack`.
  - Endpoint nuovi: `GET /api/backup` (JSON eventi), `POST /api/restore`
    (`mode: append|replace`), `POST /api/correct {key, kind: revoke|restate, price?, team?}`
    (restate = revoke + re-sell, per prezzo/squadra sbagliati).
- `tests/test_auction_store.py` (nuovo): roundtrip stato uguale; **replay
  deterministico** (stesso log → stesso stato su due motori freschi); revoke/restate
  mantengono gli invarianti; backup → import → replay uguali; scritture concorrenti
  (N thread) → seq univoci + invarianti al quiescimento; **resume**: stato pre-crash
  ricostruito da DB identico.

**Dipendenze** — WP1 (invarianti: la correttezza del replay dipende da `check_invariants`),
WP3 (eventi referenziano `pid`).

**Invarianti/decisioni da mantenere** — append-only con `supersedes` (mai DELETE dai
log: backup/import sicuri); replay deterministico = ogni evento applicato nello stesso
ordine sullo stesso config ⇒ stesso stato; per poter garantire la determinicità il
config vive dentro `league_configured`.

**Scelta conservativa (default)** — rettifica = **compensazione append-only**
(revoke/restate), non riscrittura del passato; `undo` persistito = revoke dell'ultimo
evento; percorso store default `data/asta_stagione_2026_27.db` ignorabile da `.gitignore`.

**Acceptance commands**

```
uv run pytest tests/test_auction_store.py -q
# smoke: avvia web, vendi 1 giocatore via /api/sold, uccidi il processo,
# riavvia: /api/state deve mostrare la vendita (invarianti verdi nei log)
uv run python scripts/web_auction.py --port 8123 &  # manuale, poi kill
```

**Criteri binari** — tutte le sopra test verdi; `--store` ripristina lo stato esatto
(riavvio incluso); `POST /api/correct` restate di un prezzo errato porta gli invarianti
a valere prima e dopo.

---

### WP5 — Scarsità per slot e budget delle singole squadre (roadmap #7)

**Scope**

- La scarsità oggi è **aggregata per ruolo** (`scarcity` riga 302 usa `_demand` lega).
  Aggiungere la componente **per singola squadra**: quanti team hanno ancora
  budget+slot libero nel ruolo, pesata quantitativamente (slot), qualitativamente
  (Q residua) ed economicamente (crediti disponibili vs PFC delle alternative).

**File previsti**

- `scripts/valuation.py` (nuovo): modulo di valutazione separato dal motore:
  - `team_demand(auction, role, team)`: team ha slot aperto E budget ≥ `value_floor`.
  - `competing_teams(auction, role)`: count dei team con slot aperto nel ruolo e
    budget ≥ floor (rispetto al totale iniziale).
  - `scarcity_team(auction, p, team) -> dict`: `{league: scarsità attuale (riusa
    `Auction.scarcity` riga 302), team: componente team = (competing/iniziale)^beta
    con stessi clamps di `DEFAULTS` (riga 69), quality: `quality_left(ruolo)` (riga 329),
    economic: crediti medi residui dei team concorrenti / PFC di p, breakdown finale
    bounded in `[scarcity_min, scarcity_max]`}`.
- `scripts/live_auction.py`: `evaluate` (riga 375) espone `my_team.scarcity_team`
  (wiring minimo, **senza** toccare `suggested` che resta lega-wide).
- `scripts/web_auction.py`: `player_payload` (riga 157) propaga i campi quando `team` dato.
- `tests/test_valuation.py` (nuovo): bounded e finito; team coperto (slot 0) ⇒
  componente team → 1; monotonia grossolana (vendere alternative aumenta la componente);
  lega con 1 sola squadra tracciata.

**Dipendenze** — WP1, WP2 (validazione config), WP3 (range su cui poggia `economic`),
WP4 non richiesto (può lavorare su motore in-memory).

**Invarianti/decisioni da mantenere** — `suggested` **resta** lega-wide (comparabilità
tra squadre): la componente team modula solo `my_team`/`maxbid`; clamps esistenti
`scarcity_min/max` valgono anche per il breakdown.

**Scelta conservativa (default)** — componente team applicata **solo al maxbid**
e alla sezione "per la mia rosa", mai al suggerito.

**Acceptance commands**

```
uv run pytest tests/test_valuation.py -q
uv run python scripts/live_auction.py --prezzo "CANDIDATO-TEST"  # formato invariato, campi nuovi sotto my_team
```

**Criteri binari** — `scarcity_team` bounded per ogni stato raggiungibile del demo;
test verde; payload web contiene i campi solo quando `?team=` richiesto.

---

### WP6 — Max bid: costo opportunità, riserva completamento rosa, allocazioni ruolo, alternative (roadmap #6)

**Scope**

- Il maxbid attuale (`evaluate` riga 375: `money − min_slot_price × slots_after`,
  tetto `suggested × aggression`) diventa un **modello esplicito a 4 cap** trasparente.

**File previsti**

- `scripts/valuation.py`:
  - `reserve_needed(auction, team) -> dict[role, int]`: slot aperti per ruolo ×
    **floor per ruolo** (`DEFAULTS["role_floor_price"]`, default: tutto = 2, pari al
    `min_slot_price` attuale → comportamento invariato).
  - `role_budget_left(auction, team, role)`: allocazione = budget × peso(ruolo)
    (default: peso = slot[ruolo]/Σslot) − già speso nel ruolo.
  - `opportunity_alt(auction, p, team)`: miglior alternativa rimasta nello stesso
    ruolo (max per `suggested`, solo giocatori che il team può ancora schierare).
  - `maxbid(auction, p, team) -> dict`: cap finale =
    `min(suggested × aggression, budget − reserve_needed, role_budget_left,
    alt_cap)` mai **sotto** `base` (una delle cap prese; breakdown esplicito nel payload:
    `{aggression_cap, reserve_cap, role_cap, alt_cap, final}`).
- `scripts/live_auction.py`: `evaluate` usa `valuation.maxbid`; `format_eval` (riga 544)
  mostra il breakdown (una riga per cap).
- `scripts/league_config.py`: `minimum_roster_cost` passa ai floor per ruolo (default
  piatti = comportamento WP2 invariato) → aggiornare `feasibility` di conseguenza.
- `tests/test_valuation.py`: ogni cap si attiva sullo scenario giusto (budget basso →
  reserve_cap; ruolo affollato → role_cap; alternative abbondanti → alt_cap); `final`
  mai negativo e mai sotto `base`; ruolo coperto → `maxbid = None` (compat).

**Dipendenze** — WP5 (`scarcity_team` nell'input), WP2 (floor in `league_config`).

**Invarianti/decisioni da mantenere** — nessun cap può **alzare** il tetto rispetto
alla formula attuale senza evidenza (WP9): i nuovi cap **stringono**, non allargano;
`maxbid` mai sotto `base`; `covered`/`tracked` invariate.

**Scelta conservativa (default)** — floor piatti (=2) finché WP8 non dà distribuzioni
reali di prezzo per ruolo; `alt_cap = max(suggested_alt_top × 1.0, base)` (alternativa
migliore non spinge mai oltre sé stessa).

**Acceptance commands**

```
uv run pytest tests/test_valuation.py -q
uv run python scripts/live_auction.py --demo     # demo coerente, nessun maxbid negativo
```

**Criteri binari** — breakdown a 4 cap presente in `evaluate` e nella CLI;
test verde; nessuna regressione sugli invarianti 1-6 della sezione 1.

---

### WP7 — Separazione valori: mercato / fantacalcistico / per la mia rosa (roadmap #5)

**Scope**

- Rendere **espliciti e separati** i tre valori oggi mischiati in `evaluate`:
  1. **Prezzo di mercato**: PFC, range `[lo,hi]`, PMA, `dpfcpma`, `unc_pfc` (WP3).
  2. **Valore fantacalcistico**: `expfm`, `fix_contrib`, `tit`, `tix`, `fix`, slot/fascia/status.
  3. **Valore per la mia rosa**: `suggested` (etichettato "stima mercato live"),
     `maxbid` + breakdown (WP6), `scarcity_team` (WP5), `covered`, riserve, alternative.
- Congela il **contratto di payload** consumato dal frontend (WP10 lo renderizza).

**File previsti**

- `scripts/valuation.py`: `valuation(auction, p, team) -> {"market": {...}, "fantasy": {...}, "my_team": {...}}`
  (implementa i blocchi; `market`/`fantasy` oggi già calcolabili, `my_team` combina
  WP5+WP6); nessun numero composito inventato: i tre blocchi restano disgiunti.
- `scripts/live_auction.py`: `evaluate` (riga 375) delega e ritorna i tre blocchi
  (stessa firma, chiavi nuove addizionali); `format_eval` (riga 544) riorganizza il
  testo in tre sezioni (senza perdere informazioni esistenti; etichetta chiara:
  "prezzo di mercato stimato" ≠ "max per la mia rosa").
- `scripts/web_auction.py`: `player_payload` (riga 157) espone i tre blocchi
  (chiavi `market`, `fantasy`, `my_team`).
- `tests/test_valuation.py`: **test di contratto** — `evaluate` e `/api/eval`
  contengono esattamente le chiavi dei tre blocchi; smoke del testo CLI.

**Dipendenze** — WP5, WP6 (input di `my_team`), WP3 (range/incertezza in `market`),
WP2 (config per le riserve).

**Invarianti/decisioni da mantenere** — `suggested` e `maxbid` **numerici identici**
a prima se nessun nuovo cap è attivo (WP6 è conservativo); il modello statistico FM
non compare nel blocco `market` (vedi WP9).

**Scelta conservativa (default)** — nessuna media ponderata o indice composito
"valore unico": il frontend mostra i tre blocchi separati.

**Acceptance commands**

```
uv run pytest tests/test_valuation.py -q
uv run python scripts/live_auction.py --prezzo "MCTOMINAY"   # tre sezioni leggibili
```

**Criteri binari** — contratto chiavi verde su CLI e API; i numeri `suggested/maxbid`
invariati rispetto al WP6 su uno stato di demo identico.

---

### WP8 — Calibrazione adattiva dalle vendite (roadmap #8)

**Scope**

- Imparare dalle vendite reali: fattore di mercato per **ruolo × fascia prezzo × fase
  asta**, robusto agli outlier (mediana/IQR), con **shrinkage** verso il globale e
  **confidenza** esplicita. In questo WP è **advisory**: NON entra nel prezzo finché
  il gate del WP9 non lo consente.

**File previsti**

- `scripts/calibration.py` (nuovo):
  - Input: eventi `sold` (store WP4 o `TrendAuction.events` riga 56).
  - Cell: `(ruolo, price_band, fase)`; `price_band` = bucket fissi di `base`
    (default: `<10, 10-25, 25-50, 50-100, 100-200, 200+`); `fase` = frazione venduti
    della leva (start <1/3, mid, end >2/3).
  - Statistica per cell: centro robusto = **mediana** del `premium_pfc`
    (alternativa trimmed mean al 10%); spread = MAD; **shrinkage**: fattore =
    `w·centro_cell + (1−w)·fattore_globale(ruolo)` con `w = n/(n+k)` (default `k=10`);
    confidenza = `[centro ± 1.96·MAD/√n]` clampata a banda sensata (default ±0.5).
  - Output: `factors() -> {global, per_role, per_cell: {factor, n, ci, phase, band}}`.
- `scripts/web_auction.py`: `GET /api/calibration` (nuovo); flag di config
  `use_calibration_in_price` default **false** (ritornato ma non applicato).
- `tests/test_calibration.py` (nuovo): outlier (1 vendita a 3×) non muove la mediana
  oltre soglia; n piccolo ⇒ fattore shrunk verso globale; larghezza CI ↓ al crescere
  di n; phase partition corretta; fattori bounded in `[0.5, 2.0]`.

**Dipendenze** — WP4 (log vendite), WP6 (band per ruolo/peso).

**Invarianti/decisioni da mantenere** — la calibrazione **non modifica** `suggested`
in questo WP (feature flag OFF); nessun fattore fuori banda; numeri sempre finiti.

**Scelta conservativa (default)** — advisory-only + flag OFF; si attiva solo con
evidenza del gate WP9.

**Acceptance commands**

```
uv run pytest tests/test_calibration.py -q
uv run python scripts/web_auction.py --port 8124   # GET /api/calibration -> cells+n+ci
```

**Criteri binari** — endpoint ritorna cell con `n/ci`; test verdi (incluso outlier);
flag OFF verificato da test (il suggerito non cambia con o senza vendite anomale).

---

### WP9 — Backtest distinti (prezzo d'asta e rendimento) + gate qualità (roadmap #9)

**Scope**

- Due backtest **separati** con baseline e gate espliciti; **nessuna promozione del
  modello statistico debole senza evidenza** (il modello FM resta fuori dal prezzo).

**File previsti**

- `scripts/gates.py` (nuovo): costanti e `evaluate_gate(cell) -> {pass, motivo}`.
  Default conservativi: `MIN_N_CELL=30`, `MAPE_IMPROVEMENT=0.05` (5%),
  `SPEARMAN_AUCTION_OUT=0.30` (out-of-sample), `SPEARMAN_YIELD_OUT=0.30`.
- `scripts/backtest_auction.py` (nuovo):
  - Input: `--sales PATH` (CSV degli eventi `sold`, esportabile via `GET /api/backup`
    o file `data/asta_storica.csv` con schema documentato `pid,nome,ruolo,price,team,ts,season`)
    - libro `data/listone.csv` + meta.
  - Metriche: MAE/MAPE del `suggested` vs prezzo reale, per ruolo/banda/fase;
    **coverage** (prezzo reale dentro `[suggested·0.9, suggested·1.1]`); correlazione
    Spearman ordinamento.
  - Baseline confrontate: `base` (PFC puro); `base×inflazione`; `base×inflazione×scarsità`
    (attuale); `+calibrazione` (solo se `use_calibration_in_price=true` nel flag WP8).
  - Output `data/backtest_auction_*.csv/.txt` con verdict per ruolo vs gate.
- `scripts/backtest_yield.py` (nuovo):
  - **Rendimento**: `yield = ΣFM_stagione / Σprezzo` per ruolo/banda e per squadra;
    domanda "quanto rende ciò che ho pagato". Join con `data/Stats_Rating_Stagione_2025_26.csv`
    (FM 25/26) o `data/Forward_Test_2024_25_on_FM_2025_26.csv`; se assente → proxy
    `expfm` del listone **etichettato come proxy**.
  - Baseline: mercato (PMA) e casuale; verdict vs gate.
- `scripts/live_auction.py` / `web_auction.py`: **nessun wiring del modello FM** nel
  prezzo. Il gate che può abilitare la calibrazione è registrato in
  `.agent/decisions.md` (ADR) e `scripts/gates.py`.
- `tests/test_gates.py` (nuovo): gate passa/non passa con dati sintetici ai confini.

**Dipendenze** — WP4 (log), WP8 (calibrazione da valutare), pipeline statistica
esistente (sola lettura artefatti), WP3 (pid per il join).

**Invarianti/decisioni da mantenere** — baseline SEMPRE presenti nei report; gate
valutato **out-of-sample**; il modello FM non tocca `suggested`/`maxbid`.

**Scelta conservativa (default)** — soglie di gate sopra; fintanto che il gate non
passa, `use_calibration_in_price` resta false; i report stessi sono il criterio binario.

**Acceptance commands**

```
uv run python scripts/backtest_auction.py --sales data/asta_storica.csv   # exit 0, report
uv run python scripts/backtest_yield.py                                   # exit 0, report
uv run pytest tests/test_gates.py -q
```

**Criteri binari** — entrambi gli script escono 0 e scrivono CSV+report con baseline;
ADR registrato in `.agent/decisions.md`; test: il gate NON abilita la calibrazione su
dati sotto soglia.

---

### WP10 — Frontend operativo (roadmap #10)

**Scope**

- Esporre correttamente i nuovi output e **impedire errori**: blocchi
  mercato/fantasy/rosa, scarcity per squadra, calibrazione (advisory), persistenza
  (backup/restore/rettifica), banner di fattibilità, blocco del venduto oltre il maxbid.

**File previsti**

- `scripts/static/index.html`: (nessun redesign, solo additivo)
  - Card giocatore: tre sezioni (mercato/fantasy/my-rosa) con i campi dei blocchi
    (contratto WP7); tooltip sul breakdown maxbid (4 cap); componente scarsità team;
    badge "stima mercato live" vs "max per la mia rosa".
  - Pannello calibrazione con `n` e CI, badge "advisory, non applicato al prezzo".
  - Barra persistenza: n eventi, ultimo salvataggio, bottoni ⬇ backup / ⬆ restore /
    ✎ rettifica ultima vendita (confirm).
  - Config dialog: mostra `feasibility` (errori rossi, warning arancio) e costo minimo rosa.
  - `guarda venduto`: disabilita il pulsante se `price > maxbid` spiegando quale cap è scattato.
  - **Vincolo**: restare su `textContent`/`el()` (riga ~615); niente `innerHTML`.
- `scripts/web_auction.py`: esposizione dati (WP5-8), endpoint `GET /api/backup`,
  `POST /api/restore`, `POST /api/correct` (esistono già da WP4 — qui solo assicurati
  nel contratto), `GET /api/calibration`.
- `tests/test_web_api.py` (nuovo, inizia qui e si consolida in WP11): TestClient:
  config → feasibility nella risposta → sold → correct → backup → restore →
  stato coerente; contratto chiavi di `/api/eval` (tre blocchi).

**Dipendenze** — WP2, WP4, WP5, WP6, WP7, WP8 (payload ed endpoint).

**Invarianti/decisioni da mantenere** — regola "solo textContent"; nessuna rimozione
di funzionalità esistenti (export, trend, undo); `<dialog>` nativi non toccati
esteticamente.

**Scelta conservativa (default)** — correzione solo dell'**ultima** vendita del
giocatore selezionato (revoke/restate) con conferma; restore in modalità `replace`
richiede doppia conferma e passa dalla validazione del WP4.

**Acceptance commands**

```
uv run pytest tests/test_web_api.py -q
uv run python scripts/web_auction.py --port 8125   # smoke manuale: flusso completo
grep -n innerHTML scripts/static/index.html; echo "exit=$?"   # atteso 1 (nessun output)
```

**Criteri binari** — test E2E TestClient verdi (config→vendita→rettifica→backup→restore);
nessun `innerHTML` in `index.html`; i tre blocchi renderizzano con dati freschi.

---

### WP11 — Test completi (roadmap #11)

**Scope**

- Chiudere il cerchio: unit, API, persistenza, replay, proprietà/invarianti e
  **acceptance end-to-end**. Ogni WP ha già portato i suoi test; qui consolido le
  coperture mancanti e l'E2E.

**File previsti**

- `tests/test_web_api.py` (completare da WP10): errori HTTP (400/404/409) per ogni
  endpoint; validazione config; trend; export CSV (BOM, colonne).
- `tests/test_auction_store.py` (estendere): replay con eventi misti (sold/unsold/
  revoke/restate/config), import di log dal backup, crash-resume.
- `tests/test_properties.py` (estendere): sequenze casuali sulla **versione
  persistita** (store + replay) con `check_invariants` dopo ogni evento.
- `tests/test_acceptance.py` (nuovo, E2E):
  - CLI: subprocess `uv run python scripts/live_auction.py --demo` → exit 0 e output
    attesi (inflazione x1.00 iniziale).
  - Web: TestClient — configura lega → vende tutti gli slot-obiettivo di una squadra
    → export rose/svincolati non vuoti → rettifica → backup → restore → replay identico.
- `pyproject.toml`: `[tool.pytest.ini_options] testpaths=["tests"]` e (se serve)
  `addopts="-q"`; `httpx` già in dev da WP1.

**Dipendenze** — tutti i WP precedenti (consolidamento finale).

**Invarianti/decisioni da mantenere** — ogni suite resta **deterministica** (seed,
niente rete); E2E usa dati sintetici, non il listone reale (portabilità CI).

**Scelta conservativa (default)** — niente Hypothesis come dipendenza: le proprietà
sono loop seeded su `random` (già collaudate in WP1); aggiungere Hypothesis solo se
un test specifico lo richiede esplicitamente.

**Acceptance commands**

```
uv run pytest -q                # suite completa verde
uv run pytest tests/test_acceptance.py -q
```

**Criteri binari** — suite verde end-to-end; E2E copre il ciclo vita completo e il
resume; nessun test flaky (2 run consecutivi identici).

---

### WP12 — Hardening minimo (roadmap #12)

**Scope**

- XSS sui dati dinamici, concorrenza scritture sulla persistenza, portabilità.

**File previsti**

- **XSS**:
  - `scripts/static/index.html`: mantenere il policy "solo `textContent`" (già così);
    aggiungere `tests/test_security.py`: (a) grep `innerHTML`/`insertAdjacentHTML` in
    `index.html` → deve fallire (regressione); (b) `csv_response` di
    `scripts/web_auction.py` (riga 306) **santifica i celle CSV** che iniziano con
    `= + - @` (formula injection: i dati vengono dal listone esterno) — prefisso `'`;
    test con nome giocatore malizioso `=cmd...`.
- **Concorrenza**:
  - `scripts/auction_store.py` (WP4): già `WAL` + `BEGIN IMMEDIATE` + `Lock`;
    `tests/test_auction_store.py`: stress test N thread × M sold/undo → seq univoci,
    nessun `database is locked` irrisolto, invarianti al quiescimento.
  - `scripts/web_auction.py`: il singolo processo FastAPI resta single-writer (mutex
    del store); documentare il modello in `README.md` (nessun multi-processo).
- **Portabilità**:
  - `pyproject.toml`: dipendenze complete (numpy, pytest, httpx — da WP1);
  - shebang: unificare `#!/usr/bin/env python3.10` → `#!/usr/bin/env -S uv run --quiet python`
    in `scripts/test_forward_2024_25.py`, `scripts/build_stats_rating_2025_26.py`,
    `scripts/compare_models_forward.py`, `scripts/scrape_serie_a_stats_2025_26.py`,
    `scripts/build_player_ratings.py` (comando documentato nel README);
  - encoding/path: audit `open(..., encoding="utf-8-sig")` e `os.path` (già usati).
- `README.md`: sezione "Sviluppo" (uv sync, suite test, modello di concorrenza).

**Dipendenze** — WP4 (store), WP10 (index.html finale), WP11 (infrastruttura test).

**Invarianti/decisioni da mantenere** — nessun cambiamento di comportamento: solo
santificazione, lock dichiarati, dipendenze dichiarate.

**Scelta conservativa (default)** — CSV sanitizzato con prefisso `'` (standard Excel);
niente CSP header nuovo su un'app locale (documentare come opzionale), niente
multi-processo.

**Acceptance commands**

```
uv sync                # ambiente pulito da zero
uv run pytest -q       # tutto verde su venv fresco
uv run python scripts/compare_models_forward.py   # ora gira (numpy dichiarato)
```

**Criteri binari** — `uv sync && uv run pytest -q` verde da checkout pulito;
test XSS (grep) e formula-injection verdi; stress test concorrenza verde; tutti gli
script avviabili con `uv run`.

---

## 5. Matrice dipendenze (riepilogo)

| WP | Dipende da | File a ownership esclusiva (attenzione al claim) |
| --- | --- | --- |
| WP1 | — | `pyproject.toml`, `tests/conftest.py`, `tests/test_properties.py`, `scripts/live_auction.py` (init/invarianti) |
| WP2 | WP1 | `scripts/league_config.py` (nuovo), `scripts/live_auction.py` (config), `scripts/web_auction.py` (config) |
| WP3 | WP2 | `scripts/import_listone.py`, `scripts/live_auction.py` (solo `load_players`), `data/listone.csv`+meta+report |
| WP4 | WP1, WP3 | `scripts/auction_store.py` (nuovo), `scripts/web_auction.py` (persistenza+rettifica) |
| WP5 | WP1, WP2, WP3 | `scripts/valuation.py` (nuovo, scarsità team), `scripts/live_auction.py` (wiring) |
| WP6 | WP5, WP2 | `scripts/valuation.py` (maxbid), `scripts/live_auction.py`, `scripts/league_config.py` |
| WP7 | WP5, WP6, WP3 | `scripts/valuation.py` (contratto 3 blocchi), `scripts/live_auction.py`, `scripts/web_auction.py` |
| WP8 | WP4, WP6 | `scripts/calibration.py` (nuovo), `scripts/web_auction.py` (endpoint) |
| WP9 | WP4, WP8 | `scripts/gates.py`+`backtest_auction.py`+`backtest_yield.py` (nuovi), `.agent/decisions.md` |
| WP10 | WP2, WP4, WP5-8 | `scripts/static/index.html`, `tests/test_web_api.py` |
| WP11 | tutti | `tests/test_acceptance.py` + consolidamento suite |
| WP12 | WP4, WP10, WP11 | `tests/test_security.py`, shebang, `README.md` |

---

## 6. Decisioni da registrare (ADR — `.agent/decisions.md`)

1. **Event-sourcing append-only con rettifica compensativa** (WP4): mai delete dai
   log; undo persistito = evento `revoke`; replay deterministico col config in
   `league_configured`.
2. **Il modello statistico FM non entra nel prezzo d'asta** (WP9): solo backtest con
   baseline e gate; `use_calibration_in_price=false` finché il gate non passa.
3. **Identità giocatore** = hash deterministico `norm(nome)|squadra|ruolo` con
   collisione = errore bloccante (WP3).
4. **Separazione dei tre valori senza indice composito** (WP7); `suggested` resta
   lega-wide, componente team solo su `my_team`/`maxbid` (WP5).
5. **Numpy e pytest/httpx dichiarati** come dipendenze (WP1), shebang uniformi `uv run` (WP12).

Termini da fissare in `.agent/domain.md`: `pid`, `prezzo di mercato` / `valore
fantacalcistico` / `valore per la mia rosa`, `suggested` (stima mercato live),
`rettifica`, `replay`, `fattore di mercato (calibrazione)`, `gate`, `feasibility`.

---

## 7. Condizioni di chiusura globali

1. `uv sync && uv run pytest -q` verde su ambiente pulito (una riga, binaria).
2. `uv run python scripts/import_listone.py` exit 0 e `listone.csv` con `pid`+range numerici.
3. `uv run python scripts/web_auction.py --store data/asta_stagione_2026_27.db`:
   vendite sopravvivono al riavvio; `POST /api/correct` funziona; backup/restore ok.
4. Report backtest (prezzo d'asta e rendimento) presenti con baseline e verdict gate.
5. Nessun `innerHTML` nuovo in `index.html`; nessuna variabile non dichiarata in
   `pyproject.toml`.
6. `README.md` aggiornato (config completa, persistenza, sviluppo).
