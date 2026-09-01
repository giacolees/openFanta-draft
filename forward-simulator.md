# Work package — Simulatore runtime della fase finale dell'asta Attaccanti (Monte Carlo lean)

> **Documento di specifica tecnica** (non implementazione). Riferimento: `plan.md`
> (roadmap WP1–WP12). Questo work package **non è nel piano**: è un nuovo componente
> a fianco del motore live (`scripts/live_auction.py`), progettato per essere
> implementato da **un singolo agente** con **soli file nuovi** (zero edit a file
> esistenti) e per essere **rapido da rilanciare dopo ogni acquisto live**.
>
> Regole di lavoro: mai modificare `plan.md`/`.agent/`; mai editare
> `live_auction.py`, `web_auction.py`, `league_config.py`, `import_listone.py`,
> `tests/conftest.py` (ownership di altri WP). Nessun commit. Rileggere i file
> puntati prima di implementare. Prima di chiudere: `lsp_diagnostics` +
> `lens_diagnostics mode=all` sui file nuovi + `ruff check` + `pyright`.

---

## 1. Scopo e confini

Il motore live (`Auction`) produce una valutazione puntuale (`suggested`,
`maxbid`) ma non risponde a domande **aggregate di fase finale**: *"a quanto finirà
`X`?", "ho una chance su `Y`?", "come si chiudono le rose se tutti giocano a
completare?"*. Questo WP costruisce un **simulatore Monte Carlo leggero e
interpretabile** (puro Python, niente RL, niente numpy nel hot path) che:

- parte da uno **snapshot** dello stato live (solo ruolo `A`, solo squadre
  tracciate) — `crediti residui` e `slot A residui` per squadra; per giocatore
  `base` (PFC), `pma` (media mercato), `suggested`/`scarcity` al momento dello
  snapshot, varianza opzionale (`unc_pfc` da WP3 o override esplicito), e un
  **valore/ranking separato** (input: mai nella formula di prezzo di mercato;
  il rank può modulare in modo opzionale la probabilità di interesse §4.5);
- simula ~10k aste complete della fase A con **seed deterministico**: fase
  normale con un round per giocatore (ordine casuale per run, pass opzionali) e
  fase finale di **completion forcing** (§6), willingness-to-pay campionata per
  squadra, aggiudicazione al bidder valido più alto, **max bid con riserva di
  completamento rosa**;
- **completa le rose quando matematicamente fattibile** e rende esplicito lo stato
  `infeasible` (squadra o pool) senza inventare giocatori;
- aggrega: media prezzo/giocatore (con distribuzione), probabilità team–player,
  top combinazioni finali per squadra, livello di concorrenza, scostamento
  **market vs model value senza mescolarli**.

**Fuori scope**: altri ruoli (P/D/C entrano solo come riserva conservativa),
strategie complesse (il pass è modellato in forma semplice e opzionale §4.5;
niente modelli manager),
ALTRO come acquirente, calibrazione WP8, persistenza WP4 (non serve), modifiche
al motore o al frontend.

**Modularità richiesta** (4 moduli + CLI):
`state` / `bidding` / `simulation` / `aggregation`.

---

## 2. File previsti (solo nuovi, ownership esclusiva di questo WP)

| File | Ruolo | Tocca file esistenti? |
| --- | --- | --- |
| `scripts/forward_state.py` | Stato immutabile + transizioni + snapshot da engine (duck-typed, read-only) + invarianti | No |
| `scripts/forward_bidding.py` | Prezzo atteso adattato, max bid, riserva, WTP, round d'asta, tie/no-bid | No |
| `scripts/forward_sim.py` | Driver Monte Carlo (`simulate`) + `SimResult` raw | No |
| `scripts/forward_agg.py` | `report(SimResult, snapshot, cfg) -> dict` (JSON schema v1) | No |
| `scripts/forward_simulator.py` | CLI (`--snapshot`, `--runs`, `--seed`, `--json-out`, cache) | No |
| `tests/fixtures/forward_snapshot_a.json` | Snapshot sintetico piccolo e stabile | No |
| `tests/test_forward_state.py` | Transizioni, immutabilità, invarianti | No |
| `tests/test_forward_bidding.py` | Formule bounded, riserva, tie, no-bid | No |
| `tests/test_forward_sim.py` | Determinismo, terminazione, completamento, conservazione | No |
| `tests/test_forward_agg.py` | Probabilità, combinazioni, separazione market/model | No |
| `tests/test_forward_properties.py` | Proprietà su snapshot casuali seeded | No |
| `tests/test_forward_acceptance.py` | E2E CLI su listone reale + determinismo byte-identico + performance | No |
| `data/forward_report_attaccanti.json` | Output di default (gitignorato, artato) | No |
| `data/forward_cache/` | Cache content-addressed (gitignorata) | No |
| `README.md` (sezione Simulatore) | **Opzionale**, solo se l'agente lo ritiene necessario | Sì (solo README) |

Vincolo di sequenzialità rispettato: **zero edit** ai file posseduti dagli altri WP;
le uniche letture esterne sono `league_config.DEFAULTS` (floor/min_slot_price),
`live_auction.Auction` (via duck-typing nello snapshot builder) e
`import_listone`/`load_players` (per l'acceptance su listone reale).

---

## 3. Modelli dati — API Python concreta

### 3.1 `forward_state.py`

```python
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class TeamView:
    team: str
    budget: int                          # crediti residui (int, >= 0)
    slots_a: int                         # slot A residui (int, >= 0)
    slots_other: dict[str, int] = field(default_factory=dict)  # opzionale, solo per riserva

@dataclass(frozen=True)
class PlayerView:
    pid: str
    nome: str
    base: int                            # PFC arrotondato (>= 1)
    pma: float
    unc_pfc: float | None                # semiampiezza range PFC (WP3), varianza opzionale
    suggested0: float                    # prezzo live engine allo snapshot (>= min_price)
    scarc0: float                        # scarcity engine allo snapshot (>= 0)
    unsold0: int                         # invenduti reali allo snapshot (>= 0)
    band: int                            # indice fascia prezzo (precalcolato)
    value: float | None = None           # modello valore (INPUT: mai nella formula di prezzo)
    rank: int | None = None              # ranking (INPUT: mai nel prezzo; può modulare il pass §4.5)

@dataclass(frozen=True)
class ForwardState:
    teams: tuple[TeamView, ...]
    players: tuple[PlayerView, ...]      # solo ruolo A, ordine canonico (pid)
    pool: frozenset[str]                 # pid ancora in asta
    money_league: int                    # == somma budget iniziali (lega chiusa, no ALTRO)
    # nessuna cache derivata qui: tutto ciò che cambia per run sta fuori

    def check(self) -> list[str]:        # invarianti (lista vuota = ok)
        """1. budget >= 0 per ogni squadra; slots_a >= 0.
        2. pool ⊆ players; nessun pid duplicato in players.
        3. ledger: somma(budget) + speso_totale == money_league (speso = Σ prezzi vendite).
        4. ogni valore numerico finito (math.isfinite)."""

class InvalidPurchaseError(Exception): ...
class NotInPoolError(InvalidPurchaseError): ...
class PriceBelowFloorError(InvalidPurchaseError): ...
class InsufficientBudgetError(InvalidPurchaseError): ...
class SlotUnavailableError(InvalidPurchaseError): ...

def purchase(state: ForwardState, pid: str, team: str, price: int, cfg: SimConfig) -> ForwardState:
    """Transizione IMMUTABILE: ritorna un nuovo ForwardState (copia strutturale
    O(T)); alza InvalidPurchaseError se pid non nel pool, price < min_price_for
    (config), price > budget del team, slots_a == 0. Non muta mai lo stato dato."""

def snapshot_from_auction(auction: Any, *, teams: list[str] | None = None,
                          values: dict[str, dict] | None = None) -> dict:
    """Snapshot JSON v1 dal motore live (duck-typed: usa auction.state/players/
    evaluate — sola lettura, nessun import circolare). Solo ruolo 'A' e squadre
    in auction.state['money'] (o 'teams' se dato). Per ogni giocatore nel pool A
    legge base/pma/unc_pfc dal dict player e chiama auction.evaluate(p) una sola
    volta (O(P) totale) per suggested/scarcity; unsold0 da auction.state['unsold'].
    'values': mapping pid -> {"value": float|None, "rank": int|None} (INPUT separato)."""
```

Le **transizioni avvengono solo via `purchase()`**: immutabili, testabili, e il
driver Monte Carlo lavora su copie isolate per run (nessuno stato condiviso tra
run — requisito "aggiornamento immutabile o isolato").

### 3.2 `forward_bidding.py`

```python
@dataclass(frozen=True)
class BidConfig:
    n_runs: int = 10_000
    seed: int = 42
    player_order: str = "shuffle"        # "shuffle" | "by_value" (base desc)
    floor_price: int = 2                 # floor vendita E riserva (default = min_slot_price)
    role_floor_price: dict[str, int] = field(default_factory=dict)  # hook WP6, es. {"A": 2}
    aggression: float = 1.25             # >= 1.0 (cap max offerta)
    beta_comp: float = 0.5               # peso # squadre ancora interessate
    beta_budget: float = 0.3             # peso budget medi interessate
    beta_scarc: float = 0.5              # peso scarsità comparabile (domanda/offerta fascia)
    ratio_min: float = 0.5               # clamp dei rapporti di adattamento
    ratio_max: float = 2.0
    mu_cap_factor: float = 1.25          # mu_max = mu_cap_factor * suggested0
    sigma_coef: float = 0.10             # deviazione default se manca unc_pfc
    sigma_floor: float = 1.0
    price_premium: int = 1               # incremento sopra il secondo bid (asta inglese)
    pass_base: float = 0.6               # probabilità base di pass in fase normale (§4.5)
    pass_max: float = 0.35               # tetto della probabilità di pass
    band_edges: tuple[float, ...] = (0, 5, 10, 25, 50, 100, 200)
    collect_prices: bool = True          # percentili per giocatore (lista prezzi)
    top_k: int = 5

    def validate(self) -> list[str]:
        """n_runs>=1, seed int, floor_price>=1, aggression>=1.0, 0<beta<=1,
        1.0<=ratio_min<ratio_max, mu_cap_factor>=1.0, 0<=sigma_coef<=1,
        sigma_floor>=0, premium>=0, 0<=pass_base<=1, 0<=pass_max<=1,
        band_edges crescenti, top_k>=1."""

def min_price_for(p: PlayerView, cfg: BidConfig) -> int:
    """max(1, min(floor, p.base)) — floor ruolo A = role_floor_price.get("A", floor_price).
    Per base>=floor vale floor; per base 1 vale 1 (mai 0)."""

def reserve_needed(t: TeamView, cfg: BidConfig) -> int:
    """Σ_ruolo floor_ruolo × slot_aperti_ruolo, con slot_A = slots_a e
    slot_altri = slots_other. floor_ruolo = role_floor_price.get(r, floor_price).
    Default (slots_other vuoto): floor × slots_a."""

def team_infeasible(t: TeamView, cfg: BidConfig) -> bool:
    """budget < reserve_needed(t, cfg) — non può completare: NON fa offerte."""

def eligible(t: TeamView, p: PlayerView, state: ForwardState, cfg: BidConfig) -> bool:
    """p in pool ∧ slots_a > 0 ∧ budget >= min_price_for(p) ∧ ¬team_infeasible(t).
    Eleggibilità STRUTTURALE (monotona decrescente nella run): non dipende da
    value/rank, che non entrano mai qui né nel prezzo."""

def interest_score(p: PlayerView, n_players: int, cfg: BidConfig) -> float:
    """1.0 se rank assente (fallback NEUTRO: nessun effetto); altrimenti
    clamp(0.5 + 0.5 * (1 - (rank - 1) / max(1, n_players)), 0.5, 1.0).
    Più il rank è alto (numero basso), più l'interesse è alto. NON entra mai
    nella formula di prezzo: modula solo la probabilità di pass (§4.5)."""

def pass_prob(p: PlayerView, n_players: int, cfg: BidConfig) -> float:
    """clamp(pass_base * (1 - interest_score(p)), 0.0, pass_max).
    Fallback neutro (rank assente): interest_score = 1.0 ⇒ pass_prob = 0.0
    (default = nessun pass, comportamento allineato alla v0 della spec)."""

def interested(t: TeamView, p: PlayerView, state: ForwardState,
               statics: SimStatics, cfg: BidConfig, rng: random.Random, *,
               phase: str = "normal") -> bool:
    """Interesse CAMPIONATA: eligible ∧ (phase="completion" ∨ rng.random() >= pass_prob(p)).
    Con rank assente: pass_prob = 0 ⇒ interested ≡ eligible (default).
    value/rank non entrano mai nel prezzo, solo in pass_prob (§4.5)."""

def opening_price(p: PlayerView, cfg: BidConfig) -> int:
    """Prezzo di apertura = min_price_for(p): il prezzo pagato dal bidder
    singolo (§4.4) e il pavimento di ogni vendita."""

def expected_price(p: PlayerView, state: ForwardState, statics: SimStatics,
                   cfg: BidConfig, team_budgets: dict[str, int]) -> float:
    """mu adattato (formula §4.2), clamp [min_price_for(p), mu_cap_factor*suggested0]."""

def max_bid(t: TeamView, p: PlayerView, mu: float, cfg: BidConfig) -> int | None:
    """min( round(mu*aggression), budget - reserve_needed(dopo acquisto), budget ).
    None (nessuna offerta) se il tetto scende sotto min_price_for(p)."""

def sample_wtp(rng: random.Random, mu: float, sigma: float, cfg: BidConfig) -> float:
    """round(mu + sigma * rng.gauss(0,1)) — NON clampata qui (clamp a max_bid/min nel round)."""

def conduct_round(state: ForwardState, idx: int, statics: SimStatics,
                  cfg: BidConfig, rng: random.Random, *,
                  phase: str = "normal") -> RoundOutcome:
    """Un round = una nomina (O(T)): in fase "normal" ogni squadra eleggibile
    può passare con probabilità pass_prob(p) (§4.5); in fase "completion" nessun
    pass (completion forcing §6). Aggiudica e ritorna l'esito (§4.4).
    Non muta state."""

@dataclass(frozen=True)
class RoundOutcome:
    pid: str
    phase: str                           # "normal" | "completion"
    bids: tuple[tuple[str, int], ...]    # (team, bid) ordinate desc, canonical order su parità
    winner: str | None
    price: int | None                    # None ⇔ nessuna offerta valida (no-bid)
    n_eligible: int                      # per metrica di concorrenza
    n_passed: int                        # elegibili che hanno passato (fase normal)
```

### 3.3 `forward_sim.py`

```python
@dataclass(frozen=True)
class SimStatics:
    """Derivate UNA volta dallo snapshot+cfg (O(P log P)): band per giocatore,
    mu0, sigma, min/max price, band_counts0, n_comp0(p), avg_budget0(p),
    interest_score(p), ordine squadre canonico. Niente calcoli per run."""

@dataclass(frozen=True)
class RunSummary:                        # solo con trace=True (test)
    purchases: tuple[tuple[int, str, str, str, int], ...]  # (round_no, phase, pid, team, price)
    end_pool: frozenset[str]
    end_teams: tuple[TeamView, ...]
    unfilled: tuple[str, ...]            # squadre con slots_a > 0 a fine run

@dataclass(frozen=True)
class SimResult:
    """Accumulatori raw (niente probabilità qui — le calcola forward_agg):
    per giocatore: n_sold, n_passed, n_round, sum_price, sum_price2,
      sum_competition, price_list (se cfg.collect_prices), sum_avg_budget;
    per squadra: combos Counter[frozenset[str]], unfilled_count,
      sum_budget_end, sum_slots_filled;
    shortfall_per_run: tuple[int, ...] (squadre feasibili non completate per run);
    completion_runs: int, completion_purchases: int   # stats fase di completamento (§6)
    warnings: list[str]; feasibility: dict (statico)."""

def simulate(snapshot: dict, cfg: BidConfig, *, trace: bool = False) -> SimResult:
    """Monte Carlo deterministico (§5). rng = random.Random(cfg.seed) usato in
    sequenza per TUTTE le run; stesso (seed, snapshot, cfg) ⇒ stessi numeri."""
```

### 3.4 `forward_agg.py`

```python
def report(result: SimResult, snapshot: dict, cfg: BidConfig,
           *, deterministic_report: bool = False) -> dict:
    """JSON schema v1 (§8): percentili, probabilità team–player, top combinazioni,
    concorrenza, divergenza market/model. Ordinamento canonico ovunque (pid/team);
    numeri rounded deterministicamente; deterministic_report=True ⇒ nessun
    timestamp (report byte-identico a parità di input)."""
```

### 3.5 CLI `forward_simulator.py`

```text
uv run scripts/forward_simulator.py --snapshot data/forward_snapshot_a.json \
    [--runs 10000] [--seed 42] [--player-order shuffle|by_value] \
    [--floor 2] [--json-out data/forward_report_attaccanti.json] \
    [--cache-dir data/forward_cache] [--no-cache] [--force] \
    [--deterministic-report] [--demo] [--values data/forward_values.json]
```

- `--demo`: costruisce uno snapshot sintetico "fase finale" dal listone reale
  (`load_players(data/listone.csv)` + 8 squadre 500 cr con alcune vendite A
  deterministiche) e lo stampa a video insieme a un report breve.
- exit code: `0` ok (con warning), `2` snapshot/config invalido (messaggio su
  stderr, nessun output).
- Un subcomando `snapshot` per esportare lo snapshot da un'asta live:

```text
uv run scripts/forward_simulator.py snapshot --csv data/listone.csv --squadre 8 --budget 500 \
    [--sales "DIMARCO 300 IO, PAZ N. 120 T1, ..."] --out data/forward_snapshot_a.json
```

(ricostruisce l'`Auction` dai player + vendite in formato `nome prezzo squadra`,
applica le `mark_sold`, poi `snapshot_from_auction` — il percorso "rilancio rapido
dopo ogni acquisto live" da terminale; da web lo farà un endpoint futuro, §9.)

---

## 4. Formule (semplici e bounded)

### 4.1 Termini base (da snapshot, calcolati una volta — `SimStatics`)

- `mu0(p) = suggested0` — aspettativa di prezzo di mercato allo snapshot (già
  include inflazione × scarsità × sconto invenduto del motore: **non la si
  ricalcola per run**).
- `sigma(p)` = `variance` se fornita (input), altrimenti `unc_pfc` se > 0
  (WP3), altrimenti `sigma_coef × mu0(p)`; poi `max(sigma, sigma_floor)`.
- `min_p(p) = min_price_for(p, cfg)` — pavimento di vendita e di riserva.
- `max_p(p) = round(mu_cap_factor × mu0(p))` — tetto dell'aspettativa.
- `band(p)` = indice dell'intervallo `band_edges` contenente `base(p)`
  ("fascia prezzo": giocatori comparabili = stessa fascia). `band_counts0[b]` =
  conteggio iniziale.
- `n_comp0(p)` = numero di squadre elegibili per `p` allo snapshot;
  `avg_budget0(p)` = media dei loro budget.

### 4.2 Prezzo atteso adattato (requisito: "squadre ancora interessate, budget e scarsità comparabili")

Al round di `p` nella run, con `s` lo stato corrente:

```text
n_comp(p,s)      = |squadre elegibili per p|            (O(T) per round)
n_rem_band(p,s)  = giocatori rimasti nella fascia di p   (contatore incrementale O(1))
avg_budget(p,s)  = media budget delle elegibili          (O(T) per round)

R_comp   = clamp(n_comp / n_comp0,        ratio_min, ratio_max)
R_scarc  = clamp( (n_comp / n_rem_band) / (n_comp0 / band_counts0[band]), ratio_min, ratio_max)
R_budget = clamp( avg_budget / avg_budget0, ratio_min, ratio_max )

mu(p,s) = clamp( mu0(p) × R_comp^beta_comp × R_scarc^beta_scarc × R_budget^beta_budget,
                 min_p(p), max_p(p) )
```

- Tutti i rapporti sono clampati **prima** dell'elevamento a potenza ⇒ `mu` è
  bounded per costruzione (mai sotto il pavimento, mai sopra `max_p`).
- Interpretazione: quando restano poche squadre interessate (rispetto all'inizio
  o rispetto all'offerta nella stessa fascia) e i loro budget sono bassi,
  l'aspettativa scende; la scarsità comparabile (domanda/offerta nella fascia)
  la può spingere su. È la versione *lean* della scarsità del motore, senza
  ricalcolarla a ogni vendita.
- L'eleggibilità usata qui è STRUTTURALE (mai `value`/`rank`): il pass (fase
  normale, §4.5) non modifica `mu` ma riduce il numero di offerte reali, e il
  prezzo reagisce via la regola del bidder singolo/prezzo di apertura (§4.4).

### 4.3 Max bid con riserva di completamento (requisito: "max bid fattibile con riserva completamento slot")

```text
reserve_dopo(t, p) = reserve_needed(team con slots_a - 1)     # Σ floor × slot aperti dopo l'acquisto
aggr_cap           = round(mu × aggression)
reserve_cap        = t.budget - reserve_dopo(t, p)
max_bid(t, p, mu)  = min(aggr_cap, reserve_cap, t.budget)

se max_bid < min_p(p)  ⇒  nessuna offerta (None)
```

- `team_infeasible(t) = budget < reserve_needed(t)` ⇒ la squadra **non fa mai
  offerte** in tutta la run (qualunque acquisto la renderebbe irrecuperabile);
  viene segnalata in `feasibility.teams_infeasible`.
- Per costruzione `reserve_cap ≥ min_p(p)` per ogni squadra feasibile
  (dimostrazione: `budget ≥ reserve_needed ⇒ budget − reserve_dopo ≥ floor ≥ min_p`);
  quindi una squadra feasibile può sempre almeno offrire il pavimento su un
  giocatore che può permettersi — questo garantisce il completamento (§6).
- `aggr_cap ≥ min_p` perché `aggression ≥ 1.0` e `mu ≥ min_p`.

### 4.4 Round d'asta (requisito: eligible/interested, WTP campionata, highest valid bidder)

Per ogni squadra `t` **interessata** (fase normale: eleggibile e NON in pass —
§4.5; fase completion: eleggibile, nessun pass):

```text
wtp_raw = sample_wtp(rng, mu(p,s), sigma(p))          # round(mu + sigma·z), z~N(0,1)
bid(t)  = min( max(wtp_raw, min_p(p)), max_bid(t, p, mu) )   # clamp [min_p, max_bid]
```

- **Eligible vs interested** (requisito esplicito):
  - `eligible` = condizione STRUTTURALE (§3.2): slot > 0, budget ≥ min_p,
    squadra feasibile, giocatore nel pool. Monotona decrescente nella run.
  - `interested` = `eligible` ∧ **non ha passato**: in fase `normal` ogni
    squadra eleggibile passa con probabilità `pass_prob(p)` (§4.5, campionata
    con l'RNG); in fase `completion` nessun pass. Con `value/rank` assenti
    `pass_prob = 0` ⇒ `interested ≡ eligible` (default allineato alla v0).
- **No-bid**: `winner = None` quando nessuna squadra interessata (0 elegibili in
  fase completion; in fase normal anche se tutte le elegibili passano). Il
  giocatore resta nel pool e viene ri-nominato nella fase di completamento (§6)
  se serve; altrimenti termina lì.
- **Aggiudicazione**: `winner = argmax bid` (a parità, ordine canonico delle
  squadre — tie deterministico).
- **Prezzo** (asta inglese al secondo prezzo):
  - `≥ 2` offerte: `price = max(min_p, second_bid + price_premium)` poi
    `price = min(price, bid_winner)` (mai più della propria offerta — copre i
    tie: `second == winner ⇒ price = winner`).
  - `1` offerta: `price = opening_price(p) = min_p(p)` — il bidder singolo paga
    il **prezzo di apertura/pavimento**, non la propria WTP (in un'asta inglese
    l'unico offerente non ha motivo di rilanciare sopra l'apertura; §10).
- **Aggiornamento**: `state = purchase(state, pid, winner, price, cfg)`
  (immutabile). Il prezzo è sempre `≥ min_p`, `≤ bid_winner ≤ budget − riserva`.

### 4.5 Probabilità di interesse e pass (opzionale — `value`/`rank`)

`value`/`rank` **non entrano mai nella formula di prezzo di mercato** (`mu`,
`max_bid`, `opening_price`, ammontare della WTP). In modo opzionale e bounded,
il `rank` modula la probabilità di **pass** in fase normale (e quindi le
combinazioni finali), con fallback neutro:

```text
interest_score(p) = 1.0                                          # rank assente (NEUTRO)
                 = clamp(0.5 + 0.5 * (1 - (rank-1)/max(1,n_players)), 0.5, 1.0)
pass_prob(p)     = clamp(pass_base * (1 - interest_score(p)), 0.0, pass_max)

default: pass_base = 0.6, pass_max = 0.35
```

- Rank 1 ⇒ `interest_score = 1.0` ⇒ `pass_prob = 0` (il top non viene mai passato).
- Rank peggiore ⇒ `interest_score → 0.5` ⇒ `pass_prob = min(0.3, pass_max) = 0.3`.
- `value`/`rank` assenti ⇒ `pass_prob = 0`: **nessun pass** (default, allineato
  alla v0: `interested ≡ eligible`).
- In fase `completion` il pass è sempre 0 (§6): il rank influenza solo chi si
  "risparmia" la spesa in fase normale, mai la garanzia di completamento.

### 4.6 Sconto invenduto in-sim

`mu0` include già lo sconto invenduto **reale** dello snapshot (`suggested0`
deriva da `evaluate`, che applica `unsold_discount^unsold0`). In-sim un
giocatore passato in fase normale o senza elegibili **non viene scontato**: il
meccanismo di ri-nomina è la fase di completamento (§6), che non applica sconti
aggiuntivi (semplice e interpretabile; documentato in §10).

---

## 5. Algoritmo step-by-step

### Fase A — Precompute (una volta per snapshot, O(P log P))

1. Carica snapshot → valida schema v1 (errore bloccante con lista se chiavi
   mancanti/tipi errati). Costruisci `ForwardState` iniziale (squadre, players A
   nel pool, `money_league = Σ budget`).
2. `cfg.validate()` → errore bloccante se invalido.
3. `SimStatics`: band per giocatore, `band_counts0`, `mu0`, `sigma`, `min_p`,
   `max_p`, `n_comp0`, `avg_budget0` (tutti O(P) o O(P log P)).
4. Feasibility statica (`forward_sim._static_feasibility`):
   - ogni squadra `team_infeasible`? → lista (bloccata dalle offerte);
   - `Σ slot_A delle squadre feasibili > |pool|`? → `shortfall` statico
     (deterministico: non basteranno i giocatori; la distribuzione di *chi*
     resta scoperto è per-run e va nel report);
   - warning: `ALTRO` speso nel motore escluso (lega chiusa), `value/rank`
     assenti per N giocatori, pool vuoto.

### Fase B — Per ogni run `r ∈ [0, n_runs)` (≤ 2·P·T per run)

1. RNG: unico `random.Random(cfg.seed)` consumato in sequenza (determinismo
   totale); documentato: il risultato dipende da `(seed, snapshot, cfg)`.
2. Copia isolata dello stato (le transizioni sono immutabili; il workspace per
   run contiene solo contatori incrementali: `band_rem` per fascia, niente altro).
3. **Ordine di nomina (fase normale)**: se `player_order == "shuffle"` campiona
   una permutazione dei pid nel pool con l'RNG di run; se `"by_value"` ordina per
   `base` decrescente (variante deterministica di scenario).
4. **Fase normale** — per ogni `pid` nell'ordine (round singolo, `phase="normal"`):
   a. `mu = expected_price(p, state, statics, cfg, team_budgets)` (O(T));
   b. per ogni squadra: `eligible` → campiona `pass_prob(p)` (§4.5); se
      interessata: `max_bid` → `sample_wtp` → `bid` (O(T));
   c. `conduct_round` → esito; se `winner`: `state = purchase(...)`, aggiorna
      contatori (band_rem, accumulatori, `n_passed`); altrimenti il pid resta
      nel pool.
5. **Fase di completamento** — solo se necessaria: se esiste una squadra
   **feasible** con `slots_a > 0` e il pool non è vuoto, ri-nomina i giocatori
   rimasti nel pool (stesso ordine della fase normale) con `phase="completion"`
   (`pass_prob = 0` per tutte le elegibili — completion forcing, §6) finché:
   pool vuoto, nessuna squadra feasibile con slot aperti, o un pass completo
   senza vendite (terminale: nessuna squadra eleggibile). Ogni round con ≥ 1
   elegibile vende esattamente un giocatore (pool −1).
6. Terminazione (provata, §6); invariante di fine run: `state.check() == []`.

### Fase C — Aggregazione (O(P·n_runs) solo se collect_prices, altrimenti O(P))

 1. `forward_agg.report(result, snapshot, cfg)` → dict JSON v1 (§8): percentili
    dai `price_list` (o media/varianza da `sum_price/sum_price2`), probabilità
    `team×player`, top combinazioni per squadra da `combos`, concorrenza da
    `sum_competition`, divergenza market/model da blocchi separati.

---

## 6. Completamento rose e stato infeasible

**Assioma di comportamento**: la fase normale può avere pass (§4.5, opzionali);
la **fase di completamento** è un ciclo finale di *completion forcing* in cui
**ogni squadra eleggibile fa sempre offerta** (mai pass, mai inventare giocatori,
sempre rispettando budget e riserva). Da cui, per ogni run:

1. **Mai infeasible durante la run**: ogni acquisto rispetta `budget −
   reserve_dopo ≥ 0` ⇒ una squadra feasibile allo snapshot resta feasibile
   (invariante preservato dalle transizioni, in entrambe le fasi).
2. **Terminazione**:
   - fase normale: un solo pass sull'ordine di nomina; ogni round vende al più
     un giocatore; i pass non vendono; la fase termina comunque dopo l'ultimo
     round;
   - fase di completamento: parte solo se serve (squadra feasibile con slot
     aperti e pool non vuoto); ogni round con ≥ 1 elegibile vende esattamente un
     giocatore (pool −1); l'eleggibilità è **monotona decrescente** (budget e
     slot solo in calo) ⇒ un pass completo senza vendite è terminale (nessuna
     squadra eleggibile). Round totali ≤ 2·P.
3. **Invariante di fine run (proprietà da testare)**: per ogni squadra **feasible
   con `slots_a > 0` a fine run** vale `pool == ∅` (dimostrazione: se a fine run
   esistessero una squadra feasibile con slot aperti e un giocatore nel pool, la
   fase di completamento — attiva perché la condizione di partenza era
   soddisfatta — avrebbe nominato quel giocatore nel suo ultimo round; la
   squadra sarebbe stata eleggibile — budget ≥ riserva ≥ floor ≥ min_p, slot > 0
   — e in fase completion avrebbe offerto per forza ⇒ il giocatore sarebbe stato
   venduto, assurdo). I pass della fase normale non violano l'invariante: la
   fase di completamento ri-nomina e ripara.
4. **Infeasible di pool (statico)**: se `Σ slot_A delle feasibili > |pool|` lo
   shortfall è inevitabile anche con completion forcing; il report espone
   `feasibility.shortfall` (numero) e la distribuzione per-run di quali squadre
   restano scoperte (`unfilled_prob` per squadra). Mai giocatori inventati: i
   posti vuoti restano vuoti e contano come "unfilled".
5. **Infeasible di squadra (statico)**: `budget < reserve_needed` ⇒ squadra
   bloccata (nessuna offerta, in nessuna fase), elencata in
   `feasibility.teams_infeasible` con il motivo (valori esatti). Se **tutte** le
   squadre sono infeasible → warning e report con `per_player` vuoti di vendite
   (niente crash).

---

## 7. Ordinamento/nominazione e gestione casi limite

- **Nominazione giocatori**: fase normale con un round per giocatore (ordine
  default `shuffle` — permutazione campionata per run, integra l'incertezza reale
  dell'ordine di nomina; `by_value` per scenari deterministici) e, se necessaria,
  fase di completamento che ri-nomina i rimasti nello stesso ordine (§5-6).
  Deterministico a parità di seed.
- **Ordine squadre**: canonico (ordine dello snapshot, stabile) — usato per il
  tie-break e per l'output.
- **Tie**: `argmax bid` con ordine canonico; con `price_premium` il tie al top
  paga `min(second+1, own bid) = own bid` (mai sopra la propria offerta).
- **No-bid**: 0 interessate (0 elegibili in fase completion; in fase normal anche
  con tutte le elegibili in pass) ⇒ nessuna vendita, giocatore resta nel pool e,
  se serve, viene ri-nominato in fase completion; altrimenti termina lì.
  Contatori `n_unsold`/`n_passed` per giocatore nel report.
- **Prezzo minimo**: mai sotto `min_p(p)` (pavimento = floor ruolo A, default 2,
  con `base=1 ⇒ 1`). Il bidder singolo paga esattamente `opening_price(p) =
  min_p(p)` (§4.4).
- **Numeri**: tutti i prezzi int; `mu` float solo internamente; ogni valore nel
  report è finite (test di proprietà).

---

## 8. Schema output JSON / API (schema v1)

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-09-01T09:00:00Z",          // omesso con --deterministic-report
  "seed": 42,
  "cfg": { "n_runs": 10000, "floor_price": 2, "aggression": 1.25, "beta_comp": 0.5,
           "beta_budget": 0.3, "beta_scarc": 0.5, "ratio_min": 0.5, "ratio_max": 2.0,
           "sigma_coef": 0.1, "sigma_floor": 1.0, "price_premium": 1,
           "pass_base": 0.6, "pass_max": 0.35,
           "player_order": "shuffle", "band_edges": [0,5,10,25,50,100,200], "top_k": 5 },
  "snapshot_hash": "sha256...",                    // cache key (§11)
  "feasibility": {
    "ok": true,
    "shortfall": 0,                                // Σ slot feasibili − |pool| (se > 0)
    "teams_infeasible": [ {"team": "T3", "budget": 4, "slots_a": 3, "reserve_needed": 6} ],
    "warnings": ["ALTRO speso nel motore (120 cr) escluso dalla simulazione",
                 "value/rank assenti per 34 giocatori"]
  },
  "league": { "money_league": 3200, "n_teams": 8, "n_players": 88, "n_pool0": 88 },
  "per_player": [
    {
      "pid": "a1b2...", "nome": "MALEN", "band": 6,
      "n_sold": 9420, "n_unsold": 580, "n_passed": 120, "sale_prob": 0.942,
      "avg_price": 214.3, "p10_price": 187, "p50_price": 213, "p90_price": 244,   // se collect_prices
      "avg_competition": 4.2,
      "market": { "base": 383, "pma": 389.4, "suggested_snapshot": 402,
                  "scarcity_snapshot": 1.13, "sim_avg_price": 214.3 },
      "model_value": { "value": 310.0, "rank": 1, "source": "input" },
      "divergence": { "sim_minus_base": -168.7, "sim_minus_pma": -175.1,
                      "sim_minus_value": -95.7 }        // campi SEPARATI, mai un indice unico
    }
  ],
  "per_team": [
    {
      "team": "IO", "budget0": 132, "slots_a0": 3,
      "budget_end_avg": 41.2, "slots_filled_avg": 2.9, "unfilled_prob": 0.07,
      "top_rosters": [ { "players": ["MALEN", "DIAO", "LUCCA"], "freq": 6112, "pct": 0.611 } ]
    }
  ],
  "team_player_prob": [                                // matrice P(team, pid) — prob. di acquisto
    { "team": "IO", "pid": "a1b2...", "prob": 0.12 }
  ],
  "competition": {
    "avg_eligible_overall": 3.1,
    "share_single_bidder": 0.08,                       // quota round con 1 sola offerta
    "share_two_plus": 0.92,
    "share_zero_bids": 0.03,                           // quota round senza offerte (no-bid/pass)
    "completion_phase": { "n_runs_with_phase": 2100, "n_runs_without": 7900,
                           "avg_forced_purchases": 1.4 },
    "per_band": [ { "band": 0, "avg_competition": 1.2, "n_players": 12 } ]
  },
  "warnings": [ ... ]
}
```

**Regola di separazione (WP7 in spirito, obbligatoria)**: il blocco `market`
contiene solo input di prezzo (base/PMA/suggested/scarcity/sim_avg_price); il
blocco `model_value` contiene solo `value`/`rank` **in ingresso** (mai derivati
dal prezzo dalla simulazione); `divergence` è l'unico posto in cui i delta
compaiono, **etichettati per coppia** (`sim_minus_base`, `sim_minus_pma`,
`sim_minus_value`). Nessun campo composito "valore unico".

**API web (contratto futuro, NON implementato in questo WP)**: `GET
/api/forward?runs=10000&seed=42` → stesso schema, snapshot derivato dallo stato
live del motore via `snapshot_from_auction(engine)`; `GET /api/forward/snapshot`
→ snapshot JSON. Il wiring in `web_auction.py` è rimandato (file posseduto da
altri WP); qui si consegna la funzione pura che lo renderà una riga.

---

## 9. Complessità per 10k run (puro Python, no numpy)

| Fase | Costo | Note |
| --- | --- | --- |
| Precompute statics | O(P log P) | sorting bande, una volta |
| Per run | ≤ 2·P·T | fase normale O(P·T) + fase di completamento ≤ O(P·T) (solo se serve); 88 player × 8 squadre ≈ 1.4k passi elementari |
| 10k run | ≈ 14–40M operazioni | obiettivo < 15 s su snapshot reale (88 A), bound acceptance < 45 s |
| Memoria | O(P) per run; O(P×n_runs) solo con `collect_prices` (88×10k int ≈ 7 MB, ok) | `collect_prices=false` → media/varianza online |

I contatori incrementali (fascia rimasta) rendono O(1) la scarsità; nessuna
re-istanziazione dell'`Auction` per run; `random.Random` nativo del modulo
`random` (nessuna dipendenza nuova).

---

## 10. Ambiguità risolte conservativamente (obbligo di esplicitare)

1. **Prezzo minimo**: pavimento = floor ruolo A (`role_floor_price["A"]` o
   `floor_price`, default **2** — allineato a `league_config.min_slot_price` e ai
   floor WP6); `base=1 ⇒ min 1`. Nessuna vendita sotto il pavimento, incluso il
   caso bidder singolo. Il pavimento è un **costante di dominio**, non un output.
2. **Assicurazione completamento**: riserva = Σ floor × slot aperti (A + eventuali
   `slots_other` in input). Squadra con `budget < riserva` allo snapshot =
   `infeasible` → **bloccata dalle offerte** (nessun acquisto che la renda
   irrecuperabile). Default `slots_other = {}` ⇒ la riserva copre solo gli slot A
   (conservativo rispetto ad assumere costi futuri su P/D/C sconosciuti; con
   `slots_other` in input la riserva include anche quelli).
3. **Distinzione valore/prezzo**: la **formula di prezzo di mercato** usa **solo**
   input di mercato (`base, pma, suggested0, scarc0, unc_pfc/variance`);
   `value`/`rank` **non entrano mai** in `mu`, `max_bid`, `opening_price` o
   nell'ammontare della WTP. Il `rank` può però modulare la **probabilità di
   interesse/pass** (§4.5, formula bounded e opzionale, fallback neutro) e quindi
   le combinazioni finali — mai il prezzo. `value`/`rank` appaiono solo nel blocco
   `model_value` e nei delta `divergence`; la simulazione **non deriva mai un
   valore fantacalcistico dal prezzo** (e viceversa).
4. **Pass strategico**: modellato in forma semplice e bounded (§4.5): in fase
   normale ogni squadra eleggibile passa con probabilità `pass_prob(p)`, `0`
   quando `value/rank` sono assenti (default neutro). Il pass non mette mai a
   rischio il completamento: la **fase di completion forcing** (§6) ri-nomina i
   rimasti con `pass_prob = 0`, rispettando budget/riserva e senza inventare
   giocatori. Nessun modello manager complesso (niente RL, niente regole per
   squadra).
5. **Prezzo con bidder singolo**: paga il **prezzo di apertura**
   `opening_price(p) = min_p(p)`, non la propria WTP: in un'asta inglese
   l'unico offerente non ha motivo di rilanciare sopra l'apertura (risultato
   standard della teoria). Con ≥ 2 bidder prezzo = secondo bid + `price_premium`
   (asta inglese), mai sopra l'offerta del vincitore. Nota: per la pianificazione
   budget il caso a 1 bidder ora **sottostima** (prezzo = pavimento): è il prezzo
   corretto dell'asta, e il planner lo vede esplicitamente in
   `competition.share_single_bidder`.
6. **Ordine di nomina**: shuffle per run (default) per integrare l'incertezza
   reale; `by_value` per scenari deterministici. Un solo round per giocatore:
   sufficiente per il modello (eleggibilità monotona).
7. **Tie**: ordine canonico delle squadre (deterministico, documentato);
   `price_premium` coperto dal clamp `price ≤ own bid`.
8. **Lega chiusa**: la simulazione modella solo squadre tracciate; lo speso
   `ALTRO` del motore è escluso e segnalato come warning; il ledger in-sim è
   `Σ budget finali + Σ prezzi = Σ budget iniziali`.
9. **Calibrazione WP8**: **non applicata** (nessun fattore di cell nel prezzo
   atteso); il report non contiene la calibrazione. Se in futuro servirà, sarà un
   flag esplicito in un WP separato con il gate WP9.
10. **Varianza**: default `unc_pfc` (WP3); override esplicito `variance` per
    giocatore; se assente `sigma_coef × mu0`; sempre ≥ `sigma_floor` (≥ 1 cr).

---

## 11. Dipendenze dai WP3/5/6/7/8 (e WP2) e strategia caching

### Dipendenze

| WP | Stato al momento | Uso in questo WP | Tipo |
| --- | --- | --- | --- |
| WP2 (`league_config`) | fatto | `min_slot_price`, `role_floor_price` (default), `feasibility` concetto per i floor; sola lettura | hard (read-only) |
| WP3 (identità/import) | fatto | `pid` come identità dei player; `unc_pfc` come varianza default; `load_players` per acceptance | hard (read-only) |
| WP5 (`valuation.py`) | non ancora | **non richiesto**: l'equivalente lean è `eligible`/`n_comp`/`avg_budget` (§4.2). Quando WP5 arriverà, `snapshot_from_auction` potrà riusare `competing_teams`/`scarcity_team` senza cambiare la API del simulatore | soft (seam) |
| WP6 (maxbid/riserva) | non ancora | `reserve_needed(team_view, cfg)` e `role_floor_price` replicano la semantica WP6; quando WP6 arriva si può delegare a `valuation.reserve_needed(auction, team)` via un adapter sullo stesso `BidConfig` | soft (seam) |
| WP7 (tre blocchi) | non ancora | il report segue il contratto "mai un indice composito" (`market`/`model_value`/`divergence`); `value`/`rank` sono input dall'esterno (futura pipeline WP7/WP9 o utente) e possono modulare il pass (§4.5), mai il prezzo | soft (contratto) |
| WP8 (calibrazione) | non ancora | **esclusa** dal prezzo (flag OFF per costruzione); nessun fattore applicato | nessuna |
| WP4 (store) | non ancora | non necessario: snapshot da motore in-memory | nessuna |

Seam espliciti (punti in cui un futuro WP può sostituire implementazioni locali
senza cambiare la API pubblica): `expected_price` (può consumare `scarcity_team`
WP5), `reserve_needed` (WP6), `max_bid` (WP6), `interest_score`/`pass_prob`
(input `value/rank` da WP7).

### Strategia caching/invalidation

- **Cache content-addressed**: `key = sha256(canonical_json({schema_version,
  snapshot, cfg}))` (JSON canonico: chiavi ordinate, nessun timestamp). File in
  `data/forward_cache/<key>.json` (scrittura atomica temp+rename; gitignorato);
  cache in-process (dict) per la stessa chiave nella stessa esecuzione.
- **Invalidazione**: qualsiasi variazione di snapshot **o** di config cambia la
  chiave ⇒ miss ⇒ ricalcolo completo. Un acquisto live produce uno snapshot nuovo
  ⇒ chiave nuova: **mai** dati stantii (content-addressing, non TTL).
  `--no-cache`/`--force` per test e analisi; `--deterministic-report` per
  confronti byte-identici.
- **Perché non cache incrementale**: il ricalcolo costa secondi; la complessità
  di delta-caching (aggiornare probabilità dopo una vendita) non è giustificata
  ora — documentata come ottimizzazione futura se l'uso lo richiedesse.
- **Determinismo = sicurezza della cache**: stessi (seed, snapshot, cfg) ⇒ stessi
  numeri (verificato da test di determinismo), quindi servire dalla cache è
  semanticamente identico al ricalcolo.

---

## 12. Test (unitari, proprietà, acceptance)

### 12.1 `test_forward_state.py`

- `make_snapshot(...)` helper locale (fixture in stile conftest ma confinata al
  modulo): teams/budget/slots, players A con pid.
- `purchase`: budget/slot/pool aggiornati; **immutabilità** (lo stato originale
  resta identico — frozenset/tuple); errori per: pid non nel pool, price <
  `min_price_for`, price > budget, slot esaurito.
- `check() == []` dopo sequenze arbitrarie valide; ledger conservato
  (`Σ budget + Σ prezzi == money_league`).
- `snapshot_from_auction` su un'`Auction` reale (fixture `build` di conftest o
  costruita con `load_players` sintetici): solo ruolo A, pool corretto, `suggested0`
  coerente con `auction.evaluate`, `unsold0` letto.

### 12.2 `test_forward_bidding.py`

- `min_price_for`: base 1 → 1; base ≥ floor → floor; `role_floor_price` override.
- `reserve_needed`: con/senza `slots_other`, con floor per ruolo.
- `team_infeasible`: al confine (`budget == reserve` → feasibile; `reserve − 1` →
  infeasible).
- `max_bid`: cap aggression attivo con riserva piccola; cap riserva attivo con
  budget tirato; `final == min(...)`; mai sotto `min_p` per squadre feasibili;
  `None` per squadra infeasible.
- `expected_price`: rapporti = 1 ⇒ `mu == mu0`; `n_comp` dimezzato ⇒ scala
  `0.5^beta_comp`; clamp a `min_p` e a `max_p` agli estremi dei rapporti; sempre
  finito.
- `conduct_round` con rng seeded: vincitore = bid max; con ≥ 2 offerte prezzo =
  second+`price_premium` (mai sopra own bid); con **1 offerta** prezzo =
  `opening_price` (= min_p, NON la WTP del bidder); tie → ordine canonico e
  prezzo ≤ own bid; 0 elegibili → `winner/price = None`; prezzo ≥ min_p; il
  vincitore copre il prezzo col budget.
- `interest_score`/`pass_prob`: fallback neutro (rank assente) ⇒ 1.0 / 0.0;
  rank presente ⇒ bounded in [0.5, 1.0] / [0, pass_max]; monotona (rank 1 ⇒
  pass_prob 0; rank peggiore ⇒ pass_prob più alta); mai nan.
- `conduct_round(phase="normal")` con rank: le squadre eleggibili passano con
  probabilità attesa ~pass_prob (test statistico seeded su molti round);
  `phase="completion"` ⇒ nessun pass anche con rank presente.

### 12.3 `test_forward_sim.py`

- **Determinismo**: `simulate` due volte con stesso seed → `SimResult` identici
  (confronto strutturale); seed diversi → differiscono (almeno un giocatore).
- **Terminazione**: tutte le run terminano (round finiti, niente loop).
- **Invariante completamento** (§6.3): scenario piccolo con pool ≥ domanda →
  ogni run termina con tutte le squadre feasibili a `slots_a == 0`, **anche con
  rank presente** (i pass della fase normale vengono riparati dalla fase di
  completion forcing: verificare che `completion_runs > 0` nello scenario con
  pass e che l'invariante valga comunque).
- **Fase di completamento**: scenario con rank/pass e pool ≥ domanda →
  `completion_purchases > 0`; con `value/rank` assenti (pass_prob = 0) la fase
  non parte mai (`completion_runs == 0`).
- **Shortfall**: pool < domanda → fine run con pool vuoto e `unfilled` > 0;
  la somma `unfilled` per run ≤ shortfall statico.
- Conservazione ledger per run; slot mai negativi; pool disgiunto; `trace=True`
  con `n_runs` piccolo per verifiche puntuali.
- Squadra infeasible iniziale → nessuna offerta in nessuna run; warning presente.

### 12.4 `test_forward_agg.py`

- `avg_price`/`sale_prob` corretti; `Σ_team prob(team, p) == sale_prob(p)`;
  probabilità in [0,1]; top rosters: `Σ freq + unfilled_count == n_runs`.
- Contratto: `per_player` contiene `n_passed`; `competition` contiene
  `completion_phase` con `n_runs_with_phase + n_runs_without == n_runs`.
- Blocchi `market`/`model_value`/`divergence` separati (chiavi esatte, nessuna
  chiave composita); delta etichettati.
- `deterministic_report=True` → nessun timestamp; tutti i valori finiti; rounding
  deterministico (stessi float da input identici).

### 12.5 `test_forward_properties.py`

- Loop seeded su `random` (come WP1): snapshot casuali (teams 1–8, budget 10–500,
  slots 0–6, pool variabile, player base 1–400, **metà dei casi con rank
  assegnato**) → `simulate(n_runs=200)` → invarianti §6 per ogni run (via trace),
  nessuna eccezione, output finiti; determinismo byte-identico del report (due
  simulazioni stesse → stessi numeri).

### 12.6 `test_forward_acceptance.py`

- Snapshot dal **listone reale** (`load_players(data/listone.csv)`, 8 squadre,
  vendite deterministiche per arrivare alla "fase finale", `snapshot_from_auction`)
  → `uv run python scripts/forward_simulator.py --snapshot <tmp> --runs 10000
  --seed 42 --json-out <tmp2>` → exit 0, JSON valido, chiavi dello schema v1,
  **wall time < 45 s** (bound CI, con margine; atteso < 15 s).
- Determinismo end-to-end: due run con `--deterministic-report` → file
  **byte-identici** (`cmp`).
- Snapshot invalido → exit 2 con messaggio su stderr e nessun output.
- Cache: seconda run stessa chiave → riuso file cache (meta presente); snapshot
  modificato → nuova chiave, ricalcolo.

### Acceptance commands (criteri binari)

```bash
uv run pytest tests/test_forward_state.py tests/test_forward_bidding.py \
    tests/test_forward_sim.py tests/test_forward_agg.py tests/test_forward_properties.py -q

uv run python scripts/forward_simulator.py --snapshot tests/fixtures/forward_snapshot_a.json \
    --runs 10000 --seed 42 --deterministic-report --json-out /tmp/forward_a.json
uv run python scripts/forward_simulator.py --snapshot tests/fixtures/forward_snapshot_a.json \
    --runs 10000 --seed 42 --deterministic-report --json-out /tmp/forward_b.json
cmp /tmp/forward_a.json /tmp/forward_b.json && echo OK          # byte-identici

uv run pytest tests/test_forward_acceptance.py -q
ruff check scripts/forward_*.py tests/test_forward_*.py
pyright scripts/forward_*.py
uv run pytest -q tests/test_forward_acceptance.py   # con lsp_diagnostics + lens_diagnostics mode=all prima di chiudere
```

**Criteri binari di chiusura del WP**: tutti i test verdi; determinismo
byte-identico; completamento verificato (invariante §6.3) su scenari sintetici;
`market`/`model_value` mai mescolati nel report; `< 30 s` su 10k run con snapshot
reale; nessun edit a file di altri WP (git status mostra solo file nuovi);
nessun commit.

---

## 13. Note per l'implementatore

- Rileggere `live_auction.py` (`Auction.evaluate`, `_demand`, `scarcity`,
  `check_invariants`), `league_config.py` (DEFAULTS, floor, feasibility) e
  `import_listone.py` (`compute_pid`, `unc_pfc`) prima di codificare — le firme
  citate qui sono riferimenti al momento della stesura.
- Rispettare lo stile del repo: shebang `#!/usr/bin/env -S uv run --quiet python`
  per la CLI, docstring in italiano, niente `numpy` nel hot path, niente nuove
  dipendenze (solo stdlib: `random`, `dataclasses`, `json`, `hashlib`, `argparse`).
- Mantenere i numeri interi per i prezzi e i float solo come intermediari;
  arrotondare il report con regole fisse (es. 2 decimali, `round-half-even` via
  `round()` di Python, documentato).
- Il simulatore è **read-only** rispetto al motore: se un'API del motore non
  bastasse, non modificare il motore: lavorare nello snapshot (duck-typing).
- Segnalare nel summary finale eventuali drift rispetto a `plan.md`/README.
