# openFanta-draft

Strumenti per l'asta del Fantacalcio Stagione 2026/27, basati sul **Listone Fantaculo**
(PFC, PMA, slot consigliati, expected titolarità e fantamedia).

Gestione dipendenze con **uv** (ambiente in `.venv`, versioni bloccate in `uv.lock`).

## 0. Import del listone

`uv run scripts/import_listone.py`

Legge il file `Listone_Fantaculo_*.xlsx` **più recente** in `data/` (la data è nel nome:
il file cambia ogni giorno) e produce il CSV canonico `data/listone.csv` con:
PFC e PMA (punti medi dei range, con range originali), slot consigliato, expected
titolarità, expected fantamedia, fasce, probabilità rigori/calci da fermo e due indici:

- **TIX** — indice titolarità 0-100 (percentile dell'expected titolarità dentro il ruolo)
- **FIX** — indice FM attesa 0-100 (percentile del contributo bonus a giornata:
  `(FM attesa − 6) × titolarità/100`, percentile nel ruolo)

`--file` per un file specifico, `--out` per cambiare destinazione, `--report`/`--meta`
per spostare i sidecar.

### Identità stabile (`pid`)

Il foglio non ha un ID sorgente: ogni giocatore riceve un **`pid`** deterministo e
stabile tra i refresh giornalieri = `fnv1a64(norm(nome) + '|' + ruolo)` (FNV-1a a 64 bit,
16 hex; algoritmo `pid_algorithm`/`pid_version` documentati in `listone_meta.json`).
La **squadra non entra** nel pid: l'identità sopravvive ai trasferimenti. Una
**collisione** (omonimi reali nello stesso ruolo) o una riga duplicata è un errore
**bloccante** riportato in `pid_collisions` nel report: mai unione automatica né
suffissi inventati. Il motore (`live_auction`/`web_auction`) identifica i giocatori
col pid (pool/sold/eventi), ma CLI e API **accettano ancora i nomi** in input
(`Auction.find/resolve`, `AmbiguousName` per nomi ambigui).

### Range numerici e incertezza

Le colonne nuove del CSV, oltre al `pid`:

- `pfc_lo/pfc_hi`, `pma_lo/pma_hi` — estremi numerici **parse** dai range stringa
  `pfc_range`/`pma_range` (preservati come stringhe); range sporco ⇒ `warning` +
  `None`, mai dati inventati;
- `unc_pfc` — incertezza = semiampiezza del range PFC `round((hi−lo)/2, 1)`.

### Validazione, report e uscite

- **Header obbligatori** mancanti, `pfc` non numerico/≤0, `slot` fuori 1..8,
  `tit` fuori 0..100, `expfm` fuori 0..10, duplicati e collisioni di pid ⇒ import
  **bloccato** con **exit code 2**: il CSV/meta validi precedenti **restano intatti**
  (scrittura atomica temp+rename). `pma` non positivo è solo un `warning` con
  stima = `pfc` (la media d'Italia non è mai obbligatoria: 3 giocatori del listone
  reale hanno pma=0).
- Sidecar scritti: `data/listone_meta.json` (stagione `2026-27`, `source_file`,
  `imported_at` UTC, `n_by_role`, `total_pfc_by_role`, `columns`, algoritmo/versione
  pid) e `data/import_report_<ts>.json` (`errors, warnings, n_imported, n_skipped,
  pid_collisions`).

## 1. Asta live da terminale

`uv run scripts/live_auction.py`

Prezzo dinamico = **PFC × inflazione × scarsità**, con tetto dato dal budget reale
della propria squadra:

- **Valore base** = PFC (il range suggerito resta il riferimento: "dentro il range")
- **Inflazione vs PFC e vs PMA** = crediti residui / valore residuo in carta, stimato
  rispettivamente ai prezzi suggeriti (PFC) e alla media d'Italia (PMA); normalizzate
  a ×1.00 all'inizio (la somma dei PFC supera il budget della lega: il motore misura
  quanto mercato si può davvero permettere)
- **Scarsità** = slot aperti nel ruolo vs alternative con slot consigliato ≤ al suo,
  pesata anche sul **valore** (PFC) delle alternative rimaste rispetto al suo: a parità
  di fascia, quando escono gli alternative costosi sale di più quello costoso
  (slot 1 "si schiera sempre", 2 "90% titolare", 3 "si gestisce", 4 "si evita")
- **Qualità residua ruolo** = quanta della qualità utile originaria (punti di titolarità
  ≥ 70%) è ancora in carta: scende quando escono titolari utili, in proporzione alla
  loro titolarità; i panchinari non la toccano
- **FIX med** per ruolo = contributo bonus atteso di ciò che resta in carta

Comandi: `prezzo`, `venduto`, `invenduto`, `stato`, `config`, `undo`, `aiuto`.
Alternative: `--prezzo "Nome"` per una valutazione singola, `--demo` per la simulazione.

## 2. GUI web con doppia analisi di tendenza

`uv run scripts/web_auction.py` → <http://127.0.0.1:8000>

- inserimento rapido delle offerte da browser (ricerca, prezzo precompilato, Invio = venduto)
- scheda giocatore con PFC/PMA e range, delta PFC-PMA (sotto/sovra prezzato vs Italia),
  slot consigliato, TIX/FIX, contributo atteso, rigori e calci da fermo
- **due analizzatori di tendenza**:
  - **vs PFC**: quanto stiamo pagando rispetto al prezzo suggerito Fantaculo
  - **vs PMA**: quanto stiamo pagando rispetto al prezzo medio delle aste in Italia
  - per ognuno: media mobile delle ultime 5 vendite, verdetto **IN RIALZO ↗ / STABILI → /
    IN CALO ↘** globale e per ruolo, grafico con linea di riferimento sul valore libro
- cinque workspace separati: **Asta** per le operazioni live, **Mercato** per gli
  svincolati, **Rose** per composizione e spesa, **Analisi** per stato e trend,
  **Simulatore** per gli scenari forward
- **Svincolati** consultabili direttamente, con ricerca, filtro ruolo, paginazione e
  apertura del giocatore nel tavolo d'asta; endpoint JSON `GET /api/svincolati`
- **Rose** consultabili direttamente per squadra e ruolo, con riepiloghi di budget,
  spesa e slot; ogni acquisto può essere rimosso con conferma, restituendo budget
  e slot alla squadra (`GET /api/rose`, `DELETE /api/rose/{pid}`)
- gli export CSV restano disponibili come azioni secondarie:
  `/api/export/svincolati?ruolo=P|D|C|A` e `/api/export/rose`
- **configurazione lega** (pulsante ⚙ Config): sistema **Classic o Mantra**, numero
  di squadre (2-20), crediti e nomi delle rose. Classic usa slot/formazione P-D-C-A;
  Mantra usa rosa libera (default 28, minimo 23 con almeno 2 portieri) e uno degli
  11 moduli ufficiali. La modalità d'asta può essere manuale o random; applicare
  una configurazione fa ripartire l'asta da zero (`GET/POST /api/config`).

Opzioni comuni: `--squadre 8 --budget 500 --io NOME --port 8000`.

**Persistenza event-sourced (WP4)**: di default l'asta scrive un log append-only
su SQLite (`--store data/asta_stagione_2026_27.db`); `--no-store` ripristina la
vecchia modalita' in-memory (stato perso alla chiusura). Vedi sezione "Persistenza".

### Simulatore forward della fase Attaccanti (API)

Il simulatore Monte Carlo della fase finale dell'asta (**solo modalità Classic**,
solo ruolo `A`; moduli `scripts/forward_*.py`, vedi `forward-simulator.md`) e' integrato
nell'API live come endpoint **read-only** sull'asta corrente: nessuna mutazione
dello stato, nessun impatto su prezzi e suggerimenti.

- `GET /api/forward/snapshot` — snapshot JSON v1 dello stato corrente: solo
  attaccanti nel pool, budget/slot A + `slots_other` per squadra, `state_hash`
  e `event_seq` (azioni attive). Serve a vedere esattamente cosa simulerai.
- `POST /api/forward/simulate` — Monte Carlo deterministico del resto d'asta A:

  ```bash
  curl -X POST http://127.0.0.1:8000/api/forward/simulate \
    -H 'Content-Type: application/json' \
    -d '{"runs": 10000, "seed": 42, "player_order": "shuffle", "team": "IO"}'
  ```

  Body: `runs` (default 10000, bounded 1..50000), `seed`, `player_order`
  (`shuffle` | `by_value`), `team` (squadra prospettiva, default `io`),
  `force`/`no_cache` (bypassa cache lettura o lettura+scrittura), `values`
  override opzionale `pid -> {value, rank}` (INPUT modello, mai nel prezzo).
  Risposta: `cached`, `duration_ms`, `state_hash`, `event_seq`,
  `generated_at` (UTC), `cache_key`, `values_source` e `result` (report
  schema v1: per_player con percentili di prezzo, per_team con top rose,
  team_player_prob, concorrenza, fattibilità).
- `GET /api/forward/latest` — ultimo report per lo stato CORRENTE dell'asta
  (content-addressed): un acquisto/invenduto/undo/correct/cambio config
  invalida il report e la risposta è 404 — mai report stantii.

**Valori del modello (separazione market/model, WP7)**: i valori `value`/`rank`
non entrano mai nella formula di prezzo. Se `values` non è fornito, l'API
costruisce un ranking condiviso dal blocco `fantasy.score` e il valore
monetario dal blocco `my_team.team_value` della squadra prospettiva
(`values_source: "my_team_value"`) — MAI il prezzo di mercato. Un override
esplicito prevale sempre (`values_source: "override"`).

**Cache**: content-addressed in-process, chiave = hash canonico di
(snapshot+values+config). Stessa richiesta → `cached: true` e risultato
identico; due richieste identiche concorrenti possono calcolare due volte ma
non corrompono mai la cache. Errori: 400 input invalido (runs fuori bound,
`player_order`, pid sconosciuti, squadra non tracciata, pool A vuoto),
409 stato infeasibile (shortfall o squadre senza budget di riserva). La
persistenza (store attivo o meno) non cambia la semantica: il replay
deterministico ricostruisce lo stesso `state_hash`.

## Configurazione lega (contratto unico CLI/web/dominio)

La configurazione della lega ha **un solo schema** (`scripts/league_config.py`),
condiviso da CLI (`config`), API (`GET/POST /api/config`) e dominio
(`Auction.validate_config`). Niente logica duplicata: chi applica una
configurazione *normalizza* (`normalize`), *valida strutturalmente* (`validate`)
e *verifica la fattibilità* (`feasibility`) prima di azzerare l'asta — una
configurazione rifiutata **non muta** motore né stato.

Chiavi (le opzionali senza default esplicito prendono i valori predefiniti):

| Chiave | Default | Note |
| --- | --- | --- |
| `teams` | 8 | intero 2-20 |
| `budget` | 500 | crediti per squadra, intero 10-10000 |
| `game_mode` | `classic` | `classic` oppure `mantra` |
| `roster_size` | 28 | dimensione rosa Mantra, 23-90 |
| `mantra_formation` | `4-3-3` | uno degli 11 moduli Mantra predefiniti |
| `slots` | `{P:3, D:8, C:8, A:6}` | slot Classic per ruolo (interi ≥ 0, parziali: i ruoli mancanti prendono il default) |
| `formation` | `{P:1, D:4, C:4, A:2}` | titolari per ruolo (interi ≥ 0, parziali); deve essere ≤ `slots` per ruolo |
| `tit_cov_threshold` | 70 | soglia titolarità 0-100 per "coprire" uno slot |
| `io` | `IO` | nome della propria squadra, deve comparire tra i nomi |
| `team_names` / `names` | derivati `[io, T1, …]` | nomi delle squadre: unici, tanti quanti `teams` |

In Mantra il listone conserva il ruolo Classic per prezzi/backtest e aggiunge
`ruolo_mantra` (`Por`, `Dd`, `Ds`, `Dc`, `B`, `E`, `M`, `C`, `T`, `W`, `A`,
`Pc`; multipli separati da `;`). Il matcher assegna ogni giocatore una sola volta
alla posizione che massimizza copertura e qualità: un `W;A`, per esempio, viene
valutato sia come ala sia come attaccante di raccordo. Scarsità, alternative,
valore per la propria rosa e max bid usano questa compatibilità tattica. I ruoli
mancanti non vengono inventati: il giocatore resta visibile, ma non è acquistabile
in Mantra. `data/mantra_roles.csv` integra il listone corrente senza cambiare i PID.
Si aggiorna dal file Quotazioni ufficiale con
`uv run scripts/import_mantra_roles.py data/Quotazioni_Fantacalcio_....xlsx`.

Errori **strutturali** (bloccanti): tipi/limiti delle chiavi, `formation ≤ slots`,
nomi duplicati, `io` assente. In Classic, gli errori di **fattibilità** richiedono
almeno `teams × slots[ruolo]` giocatori per ruolo; in Mantra richiedono almeno
`teams × roster_size` giocatori con ruoli ufficiali e 2 portieri per squadra. Il
budget deve sempre coprire il **costo minimo di completamento rosa**
(`minimum_roster_cost` = Σ per ruolo di `min(slots[ruolo], disponibili_ruolo)` ×
floor, default floor = `min_slot_price` = 2; WP6 introdurrà i floor per ruolo via
`role_floor_price`). I **warning** di fattibilità (margine zero su un ruolo,
budget rigido ma raggiungibile) non bloccano ma vengono esposti.

API:

- `GET /api/config` → configurazione **normalizzata** corrente, inclusi
  `game_mode`, `roster_size`, `mantra_formation` e `mantra_formations`.
- `POST /api/config` (body: `{teams, budget, names, io?, game_mode?, roster_size?,
  mantra_formation?, slots?, formation?, tit_cov_threshold?}`) → `200` con `{ok, config, feasibility: {errors,
  warnings}, minimum_roster_cost, teams, budget, names, io}`; `400` con la lista
errori se strutturalmente invalida o non fattibile (nessun reset).
- CLI Classic: `config squadre=8 budget=500 slotp=3 slotd=8 slotc=8 slota=6
  formazp=1 formazd=4 formazc=4 formaza=2 soglia-tit=70 io=IO`; CLI Mantra:
  `config sistema=mantra rosa=28 modulo=4-3-3` — valida prima di
  azzerare; su errore stampa la lista errori e l'asta resta intatta.

**Profilo engine (costruttore)**: `Auction.__init__` valida con un profilo
rilassato per le **simulazioni** — squadra singola e budget minimo ammessi
(teams ≥ 1, budget ≥ 1; le entry point pubbliche restano 2-20/10-10000) e nessuna
fattibilità di pool/budget (pool parziali). Le invarianti **strutturali** valgono
identiche al profilo pubblico: in particolare `formation ≤ slots` non viene mai
disabilitata. Le entry point (CLI, web, `validate_config`) applicano sempre il
profilo pubblico.

## Persistenza SQLite event-sourced (WP4)

L'asta e' **ripristinabile**: ogni mutazione (configurazione, vendita, invenduto,
undo/rettifica) viene registrata come **evento append-only** in un DB SQLite
(WAL, foreign keys, `schema_version`). Il motore non e' piu' l'unica fonte di
verita': e' ricostruito per **replay deterministico** del log.

### Residenti e ciclo di vita

- Avvio: `uv run scripts/web_auction.py --store data/asta_stagione_2026_27.db`
  (default). Se il DB e' vuoto viene creato il primo evento `league_configured`
  con la config CLI (`--squadre/--budget/--io`); **se il DB esiste il server
  riparte dallo stato salvato** (resume): vendite, invenduti, budget e rosa
  sono esattamente quelli dell'ultima sessione.
- `--no-store` = modalita' in-memory (undo snapshot-based come un tempo; la
  persistenze resta disattivata e gli endpoint di persistenza rispondono 409).
- **Single-process**: il lock e' in-process (`threading.RLock` + `BEGIN
  IMMEDIATE`); non avviare due server sullo stesso DB (hardening multi-process
  e' posticipato). I thread di un singolo server possono appendere in
  concorrenza senza `database locked` (seq monotone univoche).

### Tipi di evento e rettifica

- `league_configured` — configurazione normalizzata; apre un **nuovo segmento**
  (lo storico precedente resta nel log, il replay riparte dall'ultima config).
- `sold` / `unsold` — vendita / invenduto con `pid` (identita' WP3) e payload
  sufficiente (prezzo, squadra canonica, ruolo, base).
- `revoke` — **compensazione append-only**: `supersedes` punta alla `seq` della
  azione da annullare; lo storico non viene mai riscritto ne' cancellato
  (nessun DELETE del singolo evento).
- `restate` = revoke + nuova `sold` in **un comando logico atomico**.

`POST /api/undo` in persistita appende una `revoke` dell'**ultima azione attiva**
(sold/unsold non gia' revocata): niente pop silenzioso; il replay filtra gli
eventi revocati (superseded). In-memory l'undo resta snapshot-based.

### Endpoint di persistenza

- `GET /api/backup` — log eventi JSON versionato (`format`/`version`, `meta`,
  `events` con `seq/ts/type/payload/supersedes`). Pagina `/api/state` espone
  `persistence: {enabled, event_seq, last_saved, path}`.
- `POST /api/restore` `{mode: append|replace, events, meta?}` — ripristino del
  log. Il risultato viene **validato integralmente** (store temporaneo in
  memoria + replay completo) PRIMA di sostituire lo stato: su errore DB e
  motore restano intatti (400 validazione, 409 conflitto). `append` rimappa
  `seq`/`supersedes` (compatibile con l'ingresso di backup esterni).
- `POST /api/correct` `{target_seq | key, kind: revoke|restate, price?, team?}`
  — rettifica di un'azione attiva: `restate` valida la nuova vendita su un
  motore temporaneo (replay senza il target) prima di appendere.

### CLI dello store (backup/import da file)

`scripts/auction_store.py` espone la stessa logica per script: `AuctionStore`
con `append(type, payload, supersedes=None)`, `append_batch(...)` (transazione
unica, es. restate), `read_all()`, `latest()`, `last_action()`, `backup(path)`
(scrittura **atomica** temp+rename), `import_events(path, mode="append"|
"replace")` (validazione completa prima della scrittura; input invalido => DB
intatto), `close()` e context manager. `replay_engine(store, players, cls)`
ricostruisce il motore; `apply_event(auction, ev)` ri-issua un singolo evento
verificando `check_invariants()` dopo ognuno (errori contestualizzati, mai
stato parziale). Il replay parte dall'ultimo `league_configured` e riapplica i
`sold`/`unsold` attivi in ordine; **pulisce l'undo stack** (gli eventi
rivivono solo dal log). Se il `pid` di un'azione attiva non esiste più nel
listone corrente (drift), il resume aggiunge una `revoke` compensativa: il
record storico resta nel log, ma il giocatore viene rimosso dalla rosa e
prezzo/slot tornano disponibili alla squadra.

```python
uv run python - <<'EOF'
import sys; sys.path.insert(0, 'scripts')
from auction_store import AuctionStore, replay_engine
from live_auction import load_players
from web_auction import TrendAuction
s = AuctionStore('data/asta_di_test.db')
print(replay_engine(s, load_players('data/listone.csv'), TrendAuction).status())
s.close()
EOF
```

Il file di backup e' **versionato** (`version: 1`) e portabile: `replace`
rigenera anche la `meta` (incluso `league_cfg` snapshot); `append` aggiunge al
log corrente rimappando le `seq` e le `supersedes` (i riferimenti interni al
batch diventano le nuove seq; quelli al log preesistente restano). I payload
sono JSON **canonico** (chiavi ordinate): backup → import → backup sono
byte-identici.

## 3. Valutazione: tre valori separati (mercato / fantacalcistico / per la mia rosa)

Il motore distingue **tre valori disgiunti**, esposti dal contratto
`valuation_contract(auction, p, team)` e propagati da `Auction.evaluate`,
`/api/eval` e `format_eval` — **nessun indice unico**: il prezzo di mercato e
il valore per la propria rosa **non sono sinonimi** e non vengono mai fusi in
una media o in un numero composito.

```text
Mercato previsto 34–39 cr        ← quanto costa, oggi, sul mercato
Valore fantacalcistico 0.62/1    ← quanto rende, in campo (non monetario)
Valore per la rosa 42 cr         ← quanto vale per la mia rosa
Max sostenibile 40 cr            ← quanto posso permettermi (limite budget)
```

### market — prezzo di mercato atteso

- `expected`: prezzo di mercato atteso = **PFC × inflazione × scarsità ×
  sconto invenduto** — identico al `suggested` storico (mai il modello
  statistico: il rating FM non entra nel prezzo).
- `range`: il range **scala il range PFC numerico** (`pfc_lo`/`pfc_hi`) con
  gli stessi fattori live; se i numerici mancano il fallback è la semiampiezza
  `unc_pfc` attorno a `expected`, l'ultimo fallback (documentato) è ±20% di
  `expected` (`range_source` dichiara quale fonte è stata usata). Il range
  contiene **sempre** `expected` ed è sempre ≥ 1.
- PFC/PMA e range originali (`pfc_range`, `pma_range`) e numerici, `dpfcpma`,
  inflazione, scarsità, sconto invenduto, `unc_pfc` (fonte incertezza).
- Il blocco **non dipende dalla squadra** che guarda: il mercato è lo stesso
  per tutti.

### fantasy — rendimento fantacalcistico puro (non monetario)

- `utility` bounded 0–1 e `score` 0–100 (utility × 100), con il **rischio
  status già dentro** (T 1.00 / B 0.75 / P 0.50, sconosciuto 0.60).
- `expfm`, `tit`, `tix`, `fix`, `fix_contrib`, probabilità rigori/calci da
  fermo, `status`/`risk`, `slot`/`fascia`.
- **Nessun prezzo/budget dentro questo blocco** (per contratto).

### my_team — valore per la mia rosa

- `team_value = round(expected × value_ratio × marginal_gain × need)`:
  - `value_ratio` = utility del candidato / utility della **migliore
    alternativa residua** nel ruolo (clamp 0.5–2.0; nessuna alternativa ⇒ 1);
  - `marginal_gain` = guadagno marginale sulla rosa già acquistata (0–1: una
    rosa già titolare migliore lo azzera);
  - `need` = bisogno/urgenza di rosa, `clamp(1 + 0.5·buchi_titolari −
    0.1·alternative_residue)` in 0.6–1.5 (buchi titolari alzano, alternative
    abbondanti abbassano: puoi aspettare).
- Dipende da **rosa / slot / alternative**, mai dall'affordability o dal
  budget corrente: il budget limita il `maxbid`, **non** il valore intrinseco
  per la rosa (un top-up di budget cambia il max sostenibile, mai il valore).
- Ruolo coperto ⇒ `team_value = 0` con `reason: "covered"`; squadra non
  tracciata (es. ALTRO) ⇒ `None` con `reason: "untracked"`. Intero, mai
  negativo, bounded in `[0, 3×expected]`.

### maxbid — limite sostenibile (WP6), non valore

`maxbid` resta il **max sostenibile** a 4 cap (mercato × aggressione, riserva
completamento rosa, allocazione per ruolo, costo opportunità) e **può essere
al di sotto del valore per la rosa**: quando il budget non basta, paghi meno
di quanto il giocatore varrebbe per la tua rosa. `suggested` resta identico
in ogni stato; l'eventuale `maxbid < suggested` è segnalato in CLI/API.

### Contratto in codice

- `scripts/valuation.py` → `valuation_contract(auction, p, team)` (API pura,
  senza ricorsione su `evaluate`).
- `Auction.evaluate` espone i tre blocchi **additivi** (`market`, `fantasy`,
  `my_team`) mantenendo tutte le chiavi legacy (inclusi `suggested`/`maxbid`
  numerici invariati). `format_eval` stampa tre sezioni etichettate
  (Mercato previsto / Valore fantacalcistico / Valore per la mia rosa).
- `player_payload`/`/api/eval` propagano il contratto al frontend.

## Backtest distinti: prezzo d'asta e rendimento (WP9)

Due backtest separati — quello che paga e quello che rende non sono la stessa
domanda — con baseline sempre presenti e un gate di qualità binario. I report
non promuovono alcun modello: producono solo `gate_recommendation` con
`use_calibration_in_price = false`; l'attivazione richiede una decisione
esplicita futura (ADR 0002). **Nessuno storico d'asta esiste ancora nel repo**:
comandi e schemi sono pronti per quando i dati ci saranno, non viene inventata
cronaca. I file di supporto: `scripts/gates.py` (soglie e verdict puri),
`scripts/backtest_auction.py`, `scripts/backtest_yield.py`.

### Gate qualità (default conservativi, configurabili via CLI)

- `min_sample = 30` — campione out-of-sample minimo;
- `mape_improvement = 0.05` — il modello deve battere la MIGLIORE baseline di
  almeno il 5% in MAPE (solo nel backtest prezzo);
- `spearman = 0.30` — correlazione di Spearman out-of-sample minima.

Il gate non passa mai con metriche NaN/assenti, campione insufficiente,
leakage, target proxy o stessa stagione (rendimento). Ogni no-pass elenca i
motivi espliciti.

### `scripts/backtest_auction.py` — replay prequenziale del prezzo

```bash
uv run python scripts/backtest_auction.py \
    --sales data/asta_storica.csv \
    --listone data/listone.csv --teams 8 --budget 500 \
    --out data/backtest
```

Ripete le vendite **in ordine**: per ogni vendita predice il prezzo PRIMA di
applicarla (niente leakage futuro; la calibrazione è addestrata solo sulle
vendite precedenti). Modelli confrontati: `pfc` (quotazione pura), `pma`
(prezzi medi d'Italia), `live` (formula del prezzo suggerito in stato di
replay), `calibrated` (advisory WP8, mai applicata al prezzo). Metriche
MAE/RMSE/MAPE/Spearman/coverage per globale, ruolo, banda di quotazione e
fase dell'asta.

- Input `--sales`: **CSV storico** (`pid,nome,ruolo,price,team,seq,ts,season`,
  `pid` preferito, nome legacy accettato se risolve un giocatore unico;
  errori bloccanti per vendite duplicate, giocatore assente dal listone,
  prezzo non intero ≥ 1, ruolo incoerente) oppure **backup JSON** dell'event
  store (`format=openfanta-draft-events`, da `GET /api/backup`:
  gli eventi revocati sono esclusi, gli invenduti ri-applicati). Una squadra
  non presente nella config diventa `ALTRO` esplicito.
- Output atomici in `--out`: `backtest_auction_report.json/.csv/.txt`
  (contenuto deterministico, niente timestamp).
- Exit `0` = report scritti (anche con gate no-pass); exit `2` = input
  mancante/invalido o nessuna vendita (nessun artefatto scritto).

### `scripts/backtest_yield.py` — rendimento stagionale separato

```bash
uv run python scripts/backtest_yield.py \
    --sales data/asta_storica.csv \
    --outcomes data/fm_realizzate_2026_27.csv \
    --listone data/listone.csv --out data/backtest
```

Risponde a "quanto rende ciò che è stato pagato": yield = Σ FM realizzate /
Σ prezzi per squadra, ruolo e banda, più la capacità dei columni del listone
(PFC/PMA) di prevedere il rendimento realizzato (Spearman; MAE per expfm,
stessa scala fantamedia). Baseline dichiarate: PFC e PMA.

- Input `--outcomes` (CSV): `pid,nome,realized_fm,minutes,season` — `pid`
  preferito, nome fallback legacy; `realized_fm` accetta l'alias `points`;
  `season` serve a valutare l'out-of-sample.
- Se il file outcomes non ha alcuna colonna di rendimento, il target degrada
  a `expfm` del listone: è **etichettato proxy** nel report e il gate resta
  false. Stessa stagione tra vendite e outcomes (in-sample/leakage) o
  stagioni non dichiarate → gate false con motivo esplicito.
- Output atomici: `backtest_yield_report.json/.csv/.txt`; stessi exit code.

## Nota

`scripts/build_player_ratings.py` genera un rating alternativo dal file quotazioni
(`Quotazioni_Fantacalcio_*.xlsx`); non è più usato dalla pipeline dell'asta.
