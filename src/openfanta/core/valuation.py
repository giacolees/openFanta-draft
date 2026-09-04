"""Valutazione per la singola squadra: scarsità (WP5) e maxbid a 4 cap (WP6).

Modulo di valutazione SEPARATO dal motore (``openfanta.core.auction``): funzioni
pure che prendono un'istanza ``Auction`` e restituiscono misure interpretabili.
Nessun import circolare: ``openfanta.core.valuation`` importa solo
``openfanta.core.config`` (mai ``openfanta.core.auction``, che invece importa
``openfanta.core.valuation``).

La scarsità lega-wide (``Auction.scarcity``) resta invariata e continua ad
alimentare ``suggested``. Questo modulo aggiunge il punto di vista della
singola squadra, separato in quattro componenti:

- **quant** (quantitativa): domanda slot effettiva / offerta comparabile
  residua vs baseline iniziale — quanti slot aperti nel ruolo restano vs
  quante alternative di pari fascia (slot consigliato <= suo) sono ancora
  in carta.
- **qual** (qualitativa): perdita di alternative di qualità simile — stesso
  ruolo, slot <= suo, PFC entro un fattore di tolleranza e rendimento atteso
  (expfm) entro tolleranza dal suo. Misura quanto del "rendimento
  disponibile" comparabile è stato consumato.
- **econ** (economica): budget contendibile medio delle squadre concorrenti
  (crediti residui − riserva minima per completare la rosa), rapportato al
  PFC del candidato. Le squadre senza budget contendibile non contano.
- **competition**: squadre effettivamente concorrenti (slot aperto nel ruolo
  E budget contendibile >= soglia: PFC del candidato o floor del ruolo) sul
  totale delle squadre tracciate. ALTRO non è mai una squadra simulata:
  esclusa e solo contabilizzata via stato lega (``spent_unknown``).

Ogni componente espone conteggi/rapporti grezzi e un fattore finito e
bounded nei clamp di configurazione esistenti ``[scarcity_min, scarcity_max]``.
Il fattore finale (``final``, esposto da ``Auction.evaluate`` come
``scarcity_team``) è ``clamp(lega × team_component)`` con
``team_component = clamp(competition × qual × econ)``. Se il ruolo della
squadra target è coperto (slot 0) la componente team vale 1.0 (neutra) e
``covered=True``: ``scarcity_team`` coincide allora con la scarsità lega.

NESSUNA modifica a ``suggested`` (resta lega-wide, identico al pre-WP6 in
ogni stato). Il maxbid (WP6) è ora un modello esplicito a 4 cap trasparenti
in ``max_bid_breakdown``: mercato, riserva completamento rosa, allocazione
per ruolo e costo opportunità; ``final = min(cap validi)`` e PREZZO DI
MERCATO / VALORE PER LA ROSA restano concettualmente separati.

Tuning opzionale via chiavi pass-through nella config (``normalize`` le
preserva): ``scarcity_team_beta`` (competition, default 0.6),
``scarcity_qual_beta`` (default 0.5), ``scarcity_econ_beta`` (default 0.5),
``scarcity_qual_tol`` (tolleranza expfm, default 0.5),
``scarcity_qual_base_tol`` (fattore tolleranza PFC, default 2.0),
``role_budget_weights`` (pesi di allocazione per ruolo, validati),
``role_floor_price`` (floor di prezzo per ruolo, validati).

WP7 (contratto a tre blocchi): ``valuation_contract`` espone i tre valori
DISGIUNTI senza indice unico:

- **market** — prezzo di mercato atteso (``expected`` == ``suggested`` live
  invariato, mai il modello statistico FM) + incertezza: range scalato dal
  range PFC numerico con gli stessi fattori live, fallback ``unc_pfc``,
  ultimo fallback conservativo documentato (±20%). Il range contiene sempre
  ``expected`` ed e' sempre >= 1.
- **fantasy** — rendimento puro NON monetario: utility 0-1 / score 0-100,
  expfm, tit, TIX/FIX, fix_contrib, rigori/calci piazzati, status/risk,
  slot/fascia. Nessun prezzo/budget dentro il blocco.
- **my_team** — valore in crediti per la rosa (``team_value`` = expected x
  value_ratio x marginal_gain x need), separato dal maxbid sostenibile
  (WP6): dipende da rosa/slot/alternative ma NON da affordabilita' o budget
  corrente — il budget limita ``maxbid``, non il valore intrinseco per la
  rosa. Covered => 0 con reason, untracked => None.
"""

from math import isfinite

from openfanta.core.config import ROLE_ORDER
from openfanta.core.mantra import (  # pyright: ignore[reportMissingImports]
    player_roles,
    roster_spots_left,
    tactical_impact,
)

# ---- tuning: chiavi opzionali nella cfg (normalize() le preserva) ----------
TEAM_BETA = 0.6  # esponente della componente competizione
QUAL_BETA = 0.5  # esponente della componente qualitativa
ECON_BETA = 0.5  # esponente della componente economica
QUAL_TOL = 0.5  # tolleranza expfm per "qualità simile"
QUAL_BASE_TOL = 2.0  # fattore di tolleranza PFC (da metà a doppio)


def _tuning(cfg, key, default):
    """Valore di tuning dalla config, solo se numerico positivo e finito."""
    v = cfg.get(key)
    if isinstance(v, (int, float)) and isfinite(v) and v > 0:
        return v  # già numerico: nessun cast necessario
    return default


def _clamp(cfg, value):
    """Bounded nei clamp di configurazione esistenti (scarcity_min/max).

    Difensivo: un valore non finito (mai atteso, i rapporti sono guardati a
    monte) diventa neutro 1.0 prima del clamp.
    """
    if not isfinite(value):
        value = 1.0
    return max(cfg["scarcity_min"], min(cfg["scarcity_max"], value))


def _role_floor(cfg, role):
    """Floor di prezzo del ruolo: ``role_floor_price`` (WP6) o ``min_slot_price``."""
    role_floors = cfg.get("role_floor_price") or {}
    floor = role_floors.get(role, cfg.get("min_slot_price", 2))
    if isinstance(floor, (int, float)) and isfinite(floor) and floor > 0:
        return floor  # già numerico: nessun cast necessario
    return 1.0


# --------------------------------------------------------------- riserve
def reserve_floor(auction, team):
    """Riserva minima (crediti) per completare la rosa di ``team``.

    Per ogni ruolo: slot ancora aperti × floor del ruolo. Ritorna
    ``{"per_role": {ruolo: cr}, "total": cr}``. Squadre non tracciate (es.
    ALTRO) non hanno rosa da simulare: riserva zero, nessuno slot inventato.
    """
    slots = auction.state["slots"].get(team)
    per_role = dict.fromkeys(ROLE_ORDER, 0)
    total = 0
    if auction.cfg.get("game_mode") == "mantra":
        remaining = roster_spots_left(auction, team)
        total = round(remaining * _role_floor(auction.cfg, "ROSA"))
        return {"per_role": {"ROSA": total}, "total": total}
    if slots is not None:
        for role in ROLE_ORDER:
            n = slots.get(role, 0)
            if not (isinstance(n, (int, float)) and isfinite(n) and n >= 0):
                n = 0
            amt = round(n * _role_floor(auction.cfg, role))
            per_role[role] = amt
            total += amt
    return {"per_role": per_role, "total": total}


def contendible_budget(auction, team):
    """Crediti che ``team`` può davvero contendere: budget residuo − riserva
    minima per completare la rosa, mai negativo. Squadre non tracciate → 0."""
    if team not in auction.state["money"]:
        return 0
    money = auction.state["money"][team]
    reserve = reserve_floor(auction, team)["total"]
    return max(0, money - reserve)


# ------------------------------------------------------------- competizione
def _offer_threshold(auction, role, player):
    """Soglia che una squadra deve poter offrire per contare come concorrente:
    il PFC del candidato se fornito, altrimenti il floor del ruolo."""
    if player is not None:
        base = player.get("base") or player.get("pfc")
        if isinstance(base, (int, float)) and isfinite(base) and base >= 1:
            return base  # già numerico: nessun cast necessario
        return 1.0
    return _role_floor(auction.cfg, role)


def competing_teams(auction, role, player=None):
    """Squadre tracciate che possono DAVVERO competere nel ruolo ``role``:
    slot aperto nel ruolo E budget contendibile (crediti − riserva minima
    completamento rosa) >= soglia (PFC del candidato se ``player`` dato,
    altrimenti floor del ruolo).

    ALTRO non è mai una squadra simulata: le vendite ALTRO si contabilizzano
    solo via stato lega (``spent_unknown``) e non compaiono mai qui. Ritorna
    la lista delle squadre concorrenti (interpretabile: ogni voce ha team,
    money, reserve, contendible); il conteggio è ``len(risultato)``.
    """
    threshold = _offer_threshold(auction, role, player)
    out = []
    for team in auction.state["money"]:
        slots = auction.state["slots"].get(team)
        if auction.cfg.get("game_mode") == "mantra":
            if roster_spots_left(auction, team) <= 0:
                continue
        elif slots is None or slots.get(role, 0) <= 0:
            continue
        money = auction.state["money"][team]
        reserve = reserve_floor(auction, team)["total"]
        contendible = max(0, money - reserve)
        if contendible >= threshold:
            out.append(
                {
                    "team": team,
                    "money": money,
                    "reserve": reserve,
                    "contendible": contendible,
                }
            )
    return out


# ----------------------------------------------------------------- qual
def _quality_comparables(auction, p, keys) -> "tuple[float, int]":
    """Alternative di qualità simile: (somma expfm, conteggio).

    Stesso ruolo, slot consigliato <= suo, PFC entro
    [base/QUAL_BASE_TOL, base×QUAL_BASE_TOL] e expfm >= suo − tolleranza.
    Escluso il giocatore stesso. ``keys`` = pool attuale o tutti i giocatori
    (baseline iniziale).
    """
    pid = p["pid"]
    tol = _tuning(auction.cfg, "scarcity_qual_tol", QUAL_TOL)
    base_tol = _tuning(auction.cfg, "scarcity_qual_base_tol", QUAL_BASE_TOL)
    expfm = p.get("expfm") or 0.0
    base = p.get("base") or p.get("pfc") or 1.0
    lo = base / base_tol
    hi = base * base_tol
    total = 0.0
    count = 0
    for key in keys:
        if key == pid:
            continue
        q = auction.players[key]
        if not auction._same_job(p, q) or q["slot"] > p["slot"]:
            continue
        q_base = q.get("base") or 0.0
        if q_base < lo or q_base > hi:
            continue
        if (q.get("expfm") or 0.0) < expfm - tol:
            continue
        total += q.get("expfm") or 0.0
        count += 1
    return total, count


# --------------------------------------------------------------- breakdown
def scarcity_breakdown(auction, p, team=None):
    """Breakdown di scarsità per la squadra ``team`` sul candidato ``p``.

    Ritorna un dict con conteggi/rapporti grezzi e fattori (tutti finiti e
    bounded in ``[scarcity_min, scarcity_max]``) delle quattro componenti
    (quant, qual, econ, competition), la scarsità lega invariata (``league``,
    riusa ``Auction.scarcity``), la componente team e il fattore finale:

    - ``team_component`` = clamp(competition × qual × econ); vale 1.0
      (neutra) con ``covered=True`` se il ruolo della squadra target è
      coperto (slot 0);
    - ``final`` (= ``scarcity_team`` in ``Auction.evaluate``) =
      clamp(league × team_component): il fattore per la sezione "per la mia
      rosa". NESSUNA modifica a ``suggested`` né al maxbid in questo WP.

    ``team=None`` → squadra di default (``cfg["io"]``), stessa semantica
    leniente di ``Auction.evaluate``: squadra sconosciuta ⇒ non tracciata.
    """
    cfg = auction.cfg
    role = p["ruolo"]
    if team is None:
        team = cfg["io"]
    slots_team = auction.state["slots"].get(team, dict(cfg["slots"]))
    tracked = team in auction.state["money"]
    covered = tracked and (
        roster_spots_left(auction, team) <= 0
        if cfg.get("game_mode") == "mantra"
        else slots_team.get(role, 0) <= 0
    )

    # ---- league: scarsità lega attuale (invariata, riusa Auction.scarcity) ----
    league_factor = auction.scarcity(p)

    # ---- quant: domanda slot effettiva / offerta comparabile vs baseline ----
    demand = auction._player_demand(p)
    demand0 = max(
        auction.mantra_demand0[p["pid"]]
        if cfg.get("game_mode") == "mantra"
        else auction.demand0[role],
        1,
    )
    alt = max(auction._comparable_count(p), 1)
    alt0 = max(auction.alt0.get(p["pid"], 1), 1)
    quant_ratio = (demand / alt) / (demand0 / alt0)
    quant_factor = _clamp(cfg, quant_ratio ** cfg["scarcity_beta"])

    # ---- qual: perdita di alternative di qualità simile vs baseline ----
    q0, n0 = _quality_comparables(auction, p, auction.players)
    q, n = _quality_comparables(auction, p, auction.state["pool"])
    qual_ratio = q0 / max(q, 1.0)
    qual_factor = _clamp(
        cfg, qual_ratio ** _tuning(cfg, "scarcity_qual_beta", QUAL_BETA)
    )

    # ---- econ: budget contendibile medio dei concorrenti / PFC del candidato ----
    competitors = competing_teams(auction, role, p)
    n_comp = len(competitors)
    mean_contendible = (
        sum(c["contendible"] for c in competitors) / n_comp if n_comp else 0.0
    )
    base = max(p.get("base") or p.get("pfc") or 1, 1)
    econ_ratio = mean_contendible / base
    econ_factor = _clamp(
        cfg, econ_ratio ** _tuning(cfg, "scarcity_econ_beta", ECON_BETA)
    )

    # ---- competition: squadre effettivamente concorrenti / totale ----
    total = max(len(auction.state["money"]), 1)
    comp_ratio = n_comp / total
    comp_factor = _clamp(
        cfg, comp_ratio ** _tuning(cfg, "scarcity_team_beta", TEAM_BETA)
    )

    # ---- componente team + fattore finale ----
    if covered:
        team_component = 1.0
    else:
        team_component = _clamp(cfg, comp_factor * qual_factor * econ_factor)
    final_factor = _clamp(cfg, league_factor * team_component)

    return {
        "role": role,
        "team": team,
        "tracked": tracked,
        "covered": covered,
        "league": {"factor": round(league_factor, 4)},
        "quant": {
            "factor": round(quant_factor, 4),
            "ratio": round(quant_ratio, 4),
            "demand": demand,
            "demand0": demand0,
            "alt": alt,
            "alt0": alt0,
        },
        "qual": {
            "factor": round(qual_factor, 4),
            "ratio": round(qual_ratio, 4),
            "quality0": round(q0, 4),
            "quality": round(q, 4),
            "count0": n0,
            "count": n,
        },
        "econ": {
            "factor": round(econ_factor, 4),
            "ratio": round(econ_ratio, 4),
            "competitors": n_comp,
            "mean_contendible": round(mean_contendible, 2),
            "base": round(base),
        },
        "competition": {
            "factor": round(comp_factor, 4),
            "ratio": round(comp_ratio, 4),
            "competitors": n_comp,
            "total": total,
            "teams": [c["team"] for c in competitors],
        },
        "team_component": round(team_component, 4),
        "altro_spent": auction.state["spent_unknown"],
        "final": round(final_factor, 4),
    }


# ========================================================================
# WP6 — maxbid a 4 cap trasparenti: mercato, riserva, ruolo, opportunità
# ========================================================================
#
# Il maxbid legacy (``money − min_slot_price × slots_after`` con tetto
# ``suggested × aggression``) è sostituito da un modello esplicito a cap:
# PREZZO DI MERCATO (``suggested``, invariato in ogni stato) e VALORE PER
# LA ROSA (``maxbid``) restano concettualmente separati, e
# ``final = min(cap validi)``, intero, mai negativo.
#
# Quattro cap OBBLIGATORI e separati:
# - ``market_cap``:      suggested × aggression (prezzo di mercato live);
# - ``reserve_cap``:     crediti − riserva completamento rosa DOPO
#                        l'acquisto (floor per ruolo su tutti gli slot
#                        residui): nessun maxbid può rendere
#                        matematicamente impossibile finire la rosa;
# - ``role_cap``:        budget residuo dell'allocazione per ruolo
#                        (target + rilasci dei ruoli completati − speso);
# - ``opportunity_cap``: costo opportunità vs la migliore alternativa
#                        residua (utility fantacalcistica separata, prezzo
#                        live dell'alternativa, urgenza/scarsità).
#
# NIENTE ML e niente ``Auction.evaluate`` ricorsivo: ``live_price`` replica
# la formula del suggerito (base × inflazione × scarsità × sconto invenduto)
# che è l'unico "prezzo di mercato live" del dominio.
#
# Se la squadra non può / sceglie di non sostenere il PFC, il max
# sostenibile può stare SOTTO ``base``: NON si forza ``final >= base``.
# Ruolo coperto o squadra non tracciata ⇒ ``maxbid = None`` (nessun tetto
# calcolabile, compat con il comportamento pre-WP6).

UTIL_WEIGHTS = {
    "tit": 0.35,  # titolarità attesa
    "expfm": 0.25,  # FM attesa
    "tix": 0.15,  # percentile titolarità nel ruolo (indice del listone)
    "fix": 0.15,  # percentile bonus nel ruolo (indice del listone)
    "fix_contrib": 0.10,  # contributo bonus atteso per giornata
}
STATUS_RISK = {"T": 1.0, "B": 0.75, "P": 0.5}
STATUS_RISK_DEFAULT = 0.6  # status sconosciuto: rischio moderato
EXP_LOW, EXP_HIGH = 6.0, 8.0  # normalizzazione FM attesa: 6.0 → 0, 8.0 → 1
FC_LOW, FC_HIGH = -0.5, 1.5  # normalizzazione contributo: -0.5 → 0, 1.5 → 1
BENCH_FACTOR = 0.5  # rotazione/panchina vale metà del guadagno marginale
RATIO_MIN, RATIO_MAX = 0.5, 2.0  # clamp del rapporto di utility (opportunità)
URG_MIN, URG_MAX = 1.0, 1.5  # clamp del fattore di urgenza (opportunità)
ALT_ABUNDANCE_STEP = 0.1  # sconto di urgenza per ogni alternativa residua


def _bounded(v, lo, hi):
    """Clamp generico (nessun conflitto con ``_clamp(cfg, value)`` di WP5,
    che è bounded dai clamped di scarsità della config). Valori non finiti
    → estremo inferiore (difensivo)."""
    if not isfinite(v):
        return lo
    return max(lo, min(hi, v))


def fantasy_utility(p) -> float:
    """Utility fantacalcistica PURA del giocatore, bounded in [0, 1].

    Dati del listone (nessun modello statistico, nessun prezzo):
    - tit = titolarità attesa / 100;
    - expfm = FM attesa normalizzata: ``(expfm − 6.0) / 2.0`` (6.0 → 0,
      8.0 → 1);
    - tix, fix = percentile ruolo / 100 (indici già calcolati dal listone);
    - fix_contrib = contributo bonus normalizzato: ``(fix_contrib + 0.5)/2.0``;
    - rischio status: T → 1.0, B → 0.75, P → 0.5, sconosciuto → 0.6.

    ``utility = (0.35·tit + 0.25·expfm + 0.15·tix + 0.15·fix + 0.10·fix_contrib)
    × rischio``. Il rischio MOLTIPLICA: uno status incerto riduce ogni
    contributo (monotona rispetto a tit/expfm/status).
    """
    tit = _bounded((p.get("tit") or 0.0) / 100.0, 0.0, 1.0)
    expfm = _bounded(
        ((p.get("expfm") or EXP_LOW) - EXP_LOW) / (EXP_HIGH - EXP_LOW), 0.0, 1.0
    )
    tix = _bounded((p.get("tix") or 0.0) / 100.0, 0.0, 1.0)
    fix = _bounded((p.get("fix") or 0.0) / 100.0, 0.0, 1.0)
    fc = _bounded(
        ((p.get("fix_contrib") or FC_LOW) - FC_LOW) / (FC_HIGH - FC_LOW), 0.0, 1.0
    )
    base = (
        UTIL_WEIGHTS["tit"] * tit
        + UTIL_WEIGHTS["expfm"] * expfm
        + UTIL_WEIGHTS["tix"] * tix
        + UTIL_WEIGHTS["fix"] * fix
        + UTIL_WEIGHTS["fix_contrib"] * fc
    )
    risk = STATUS_RISK.get(str(p.get("status") or "").upper(), STATUS_RISK_DEFAULT)
    return _bounded(base * risk, 0.0, 1.0)


def live_price(auction, p) -> int:
    """Prezzo di mercato live del giocatore: STESSA formula di ``suggested``
    (``base × inflazione × scarsità × sconto invenduto``, arrotondato, mai
    sotto 1). Replica il calcolo del motore SENZA chiamare
    ``Auction.evaluate`` (niente ricorsione): il modello statistico FM non
    entra mai nel prezzo. La formula resta quella pre-WP6."""
    cfg = auction.cfg
    disc = cfg["unsold_discount"] ** auction.state["unsold"].get(p["pid"], 0)
    return max(1, round(p["base"] * auction.inflation() * auction.scarcity(p) * disc))


def _num_or(v, default):
    """Numero finito (int/float) o default: difensivo, MAI eccezioni su input
    sporchi (le chiamate int()/float() non controllate sono bandite)."""
    if isinstance(v, (int, float)) and isfinite(v):
        return v
    return default


def _purchased_in_role(auction, team, role) -> int:
    """Quanti giocatori la squadra ha già comprato nel ruolo (differenza
    slot iniziali − slot residui). Squadra non tracciata → 0."""
    slots = auction.state["slots"].get(team)
    if slots is None:
        return 0
    return auction.cfg["slots"][role] - max(0, _num_or(slots.get(role), 0))


def _purchased_utilities(auction, team, role):
    """Utility fantacalcistiche dei giocatori già acquistati dalla squadra
    nel ruolo (per il confronto col peggior titolare schierato)."""
    out = []
    for key, _price, t, r in auction.state["sold"]:
        if t == team and r == role and key in auction.players:
            out.append(fantasy_utility(auction.players[key]))
    return out


def roster_marginal_gain(auction, p, team) -> float:
    """Rendimento marginale (bounded in [0, 1]) del candidato rispetto ai
    titolari previsti dalla formazione, data la qualità già acquistata.

    - ``filled < formation[ruolo]``: mancano titolari nel ruolo → il
      candidato riempie un buco da titolare: gain = utility piena.
    - ``filled >= formation[ruolo]``: rosa già titolare nel ruolo → il
      candidato è rotazione/panchina: gain = ``max(0, utility(cand) −
      utility del peggior titolare già acquistato) × BENCH_FACTOR`` (la
      rotazione vale la metà del guadagno). Una rosa forte (peggior acquisto
      di utility alta) riduce il gain marginale.

    Squadra non tracciata → 0 (nessun guadagno simulabile).
    """
    if team not in auction.state["money"]:
        return 0.0
    if auction.cfg.get("game_mode") == "mantra":
        return _bounded(
            tactical_impact(auction, p, team, fantasy_utility)["marginal_gain"],
            0.0,
            1.0,
        )
    role = p["ruolo"]
    formation = auction.cfg["formation"][role]
    filled = _purchased_in_role(auction, team, role)
    util = fantasy_utility(p)
    if filled >= formation and filled > 0:
        purchased = _purchased_utilities(auction, team, role)
        worst = min(purchased) if purchased else 0.0
        return _bounded(max(0.0, util - worst) * BENCH_FACTOR, 0.0, 1.0)
    return _bounded(util, 0.0, 1.0)


def best_alternative(auction, p, team):
    """Migliore alternativa residua che la squadra può ancora schierare:
    stesso ruolo, ancora nel pool, escluso il candidato, con uno slot aperto
    della squadra nel ruolo (ruolo coperto → nessuna alternativa → None).

    Ordinamento deterministico: utility fantacalcistica discendente, a
    parità PFC maggiore, a parità pid (ordine alfabetico) — mai dipendente
    dall'ordine di iterazione dei set. Ritorna il dict giocatore o None."""
    role = p["ruolo"]
    slots = auction.state["slots"].get(team)
    if auction.cfg.get("game_mode") == "mantra":
        if roster_spots_left(auction, team) <= 0:
            return None
    elif slots is None or slots.get(role, 0) <= 0:
        return None
    best = None
    best_key = (-1.0, -1.0, "")
    for key in auction.state["pool"]:
        if key == p["pid"]:
            continue
        q = auction.players[key]
        if not auction._same_job(p, q):
            continue
        q_key = (fantasy_utility(q), q.get("base") or 0.0, key)
        if q_key > best_key:
            best = q
            best_key = q_key
    return best


def reserve_after_purchase(auction, p, team):
    """Riserva minima (crediti) per completare la rosa DOPO l'acquisto del
    candidato ``p``: per ogni ruolo slot ancora aperti × floor del ruolo
    (``role_floor_price`` o ``min_slot_price``), con lo slot del ruolo del
    candidato già consumato (solo se squadra tracciata e ruolo non coperto).

    Ritorna ``{"per_role": {ruolo: cr}, "total": cr}``. Squadre non tracciate
    (es. ALTRO) → zero (nessuno slot inventato). Nessun maxbid può rendere
    matematicamente impossibile completare la rosa: il cap riserva =
    ``crediti − total`` resta >= 0 per costruzione del cap."""
    slots = auction.state["slots"].get(team)
    per_role = dict.fromkeys(ROLE_ORDER, 0)
    total = 0
    if auction.cfg.get("game_mode") == "mantra":
        remaining = max(0, roster_spots_left(auction, team) - 1)
        total = round(remaining * _role_floor(auction.cfg, "ROSA"))
        return {"per_role": {"ROSA": total}, "total": total}
    if slots is not None:
        for role in ROLE_ORDER:
            n = slots.get(role, 0)
            if role == p["ruolo"] and n > 0:
                n -= 1
            if not (isinstance(n, (int, float)) and isfinite(n) and n >= 0):
                n = 0
            amt = round(n * _role_floor(auction.cfg, role))
            per_role[role] = amt
            total += amt
    return {"per_role": per_role, "total": total}


def _role_weights(auction):
    """Pesi per ruolo dell'allocazione del budget (somma = 1).

    Override dalla config ``role_budget_weights`` ({ruolo: peso >= 0,
    somma > 0}, validata da ``league_config.validate``; ruoli mancanti → 0);
    altrimenti derivati dalla composizione iniziale del PFC
    (``auction.role_pool0``): quanto del valore in carta appartiene a ciascun
    ruolo. Nessun valore distinguibile → pesi uniformi (difensivo).
    """
    cfg = auction.cfg
    over = cfg.get("role_budget_weights")
    if over:
        weights = {r: max(0.0, _num_or(over.get(r), 0.0)) for r in ROLE_ORDER}
        total = sum(weights.values())
        if isfinite(total) and total > 0:
            return {r: w / total for r, w in weights.items()}
    pool = auction.role_pool0
    total = sum(max(0.0, _num_or(pool.get(r), 0.0)) for r in ROLE_ORDER)
    if not (isfinite(total) and total > 0):
        return {r: 1.0 / len(ROLE_ORDER) for r in ROLE_ORDER}
    return {r: max(0.0, _num_or(pool.get(r), 0.0)) / total for r in ROLE_ORDER}


def role_budget_left(auction, team, role):
    """Budget residuo spendibile nel ruolo ``role`` (allocazione WP6).

    - ``weights``: ``role_budget_weights`` dalla config (validata) o derivati
      dalla composizione PFC iniziale (``role_pool0``);
    - ``target`` = round(budget iniziale × peso[ruolo]);
    - ``spent`` = crediti già spesi dalla squadra nel ruolo;
    - ``released`` = quota del budget inutilizzato dei ruoli GIÀ COMPLETATI
      (slot residui = 0) ridistribuita a QUESTO ruolo in proporzione al peso
      (i ruoli aperti si dividono il rilascio);
    - ``left`` = max(0, target + released − spent): il cap di ruolo.

    Squadra non tracciata → None (nessuna allocazione simulabile).
    """
    if team not in auction.state["money"]:
        return None
    cfg = auction.cfg
    if cfg.get("game_mode") == "mantra":
        reserve = reserve_floor(auction, team)["total"]
        left = max(0, auction.state["money"][team] - reserve)
        return {
            "role": "/".join(player_roles({"ruolo": role})),
            "team": team,
            "source": "mantra_flexible_roster",
            "weights": {},
            "target": cfg["budget"],
            "spent": cfg["budget"] - auction.state["money"][team],
            "released": 0,
            "left": left,
        }
    slots = auction.state["slots"][team]
    weights = _role_weights(auction)
    budget = cfg["budget"]
    target = {r: round(budget * weights[r]) for r in ROLE_ORDER}
    spent: dict[str, float] = dict.fromkeys(ROLE_ORDER, 0.0)
    for _key, price, t, r in auction.state["sold"]:
        if t == team and r in spent and isinstance(price, (int, float)):
            spent[r] += price
    open_roles = [r for r in ROLE_ORDER if slots.get(r, 0) > 0]
    released_pool = sum(
        max(0.0, target[r] - spent[r]) for r in ROLE_ORDER if r not in open_roles
    )
    if role in open_roles:
        w_open = sum(weights[r] for r in open_roles) or 1.0
        released_here = round(released_pool * weights[role] / w_open)
    else:
        released_here = 0
    allocated = target[role] + released_here
    left = max(0, round(allocated - spent[role]))
    return {
        "role": role,
        "team": team,
        "source": "config" if cfg.get("role_budget_weights") else "pfc_composition",
        "weights": {r: round(weights[r], 4) for r in ROLE_ORDER},
        "target": target[role],
        "spent": round(spent[role]),
        "released": released_here,
        "left": left,
    }


def opportunity_cap(auction, p, team, alt=None):
    """Cap per costo opportunità (WP6): quanto ha senso spendere per ``p``
    invece di prendere la migliore alternativa residua.

    ``opportunity_cap = round(alt_price × value_ratio × urgency)``:
    - ``alt_price``: prezzo di mercato live dell'alternativa (``live_price``,
      mai il modello statistico);
    - ``value_ratio = clamp(utility(p) / max(utility(alt), eps), 0.5, 2.0)``:
      un candidato chiaramente migliore alza il cap sopra l'alternativa; uno
      peggiore lo abbassa (mai sotto metà);
    - ``urgency = clamp(1 + 0.5·holes_ratio − 0.1·n_alt
      + 0.25·clamp(scarcity_team − 1, −0.4, 0.4), 1.0, 1.5)``:
      * ``holes_ratio = max(0, formation[ruolo] − acquisti squadra nel
        ruolo) / max(formation[ruolo], 1)``: mancano titolari → più urgente
        (una rosa già titolare non alza l'urgenza);
      * ``n_alt`` = alternative residue nello stesso ruolo: più abbondanti →
        puoi aspettare → cap più stretto;
      * ``scarcity_team`` (WP5): meno competizione/qualità residua → più
        urgente. Tutti i fattori sono bounded.

    Nessuna alternativa → ``cap = None`` (niente costo opportunità: il cap
    non stringe). Ritorna un dict interpretabile (alternative, ratio,
    urgenza, fattori grezzi)."""
    if alt is None:
        alt = best_alternative(auction, p, team)
    cand_util = fantasy_utility(p)
    if alt is None:
        return {
            "cap": None,
            "alternative": None,
            "candidate_utility": round(cand_util, 4),
            "value_ratio": None,
            "urgency": None,
            "holes_ratio": None,
            "n_alt": 0,
            "scarcity_team": None,
        }
    alt_util = fantasy_utility(alt)
    ratio = _bounded(cand_util / max(alt_util, 1e-9), RATIO_MIN, RATIO_MAX)
    role = p["ruolo"]
    if auction.cfg.get("game_mode") == "mantra":
        impact = tactical_impact(auction, p, team, fantasy_utility)
        holes_ratio = _bounded(len(impact["holes_before"]) / 11, 0.0, 1.0)
        n_alt = sum(
            1
            for k in auction.state["pool"]
            if k != p["pid"] and auction._same_job(p, auction.players[k])
        )
    else:
        formation = auction.cfg["formation"][role]
        filled = _purchased_in_role(auction, team, role)
        holes_ratio = _bounded(max(0, formation - filled) / max(formation, 1), 0.0, 1.0)
        n_alt = sum(
            1
            for k in auction.state["pool"]
            if k != p["pid"] and auction.players[k]["ruolo"] == role
        )
    st = scarcity_breakdown(auction, p, team)["final"]
    urgency = _bounded(
        1.0
        + 0.5 * holes_ratio
        - ALT_ABUNDANCE_STEP * n_alt
        + 0.25 * _bounded(st - 1.0, -0.4, 0.4),
        URG_MIN,
        URG_MAX,
    )
    alt_price = live_price(auction, alt)
    cap = max(1, round(alt_price * ratio * urgency))
    return {
        "cap": cap,
        "alternative": {
            "pid": alt["pid"],
            "nome": alt["nome"],
            "prezzo": alt_price,
            "utility": round(alt_util, 4),
            "base": alt["base"],
        },
        "candidate_utility": round(cand_util, 4),
        "value_ratio": round(ratio, 4),
        "urgency": round(urgency, 4),
        "holes_ratio": round(holes_ratio, 4),
        "n_alt": n_alt,
        "scarcity_team": round(st, 4),
    }


def max_bid_breakdown(auction, p, team=None):
    """Breakdown completo del maxbid (WP6): 4 cap separati e trasparenti.

    - ``market_cap``: round(suggested × aggression) — prezzo di mercato live
      (l ``suggested`` lega-wide resta IDENTICO al pre-WP6) per
      l'aggressività configurata;
    - ``reserve_cap``: crediti − riserva completamento rosa DOPO l'acquisto
      (floor per ruolo su tutti gli slot residui);
    - ``role_cap``: budget residuo dell'allocazione per ruolo (target +
      rilasci dei ruoli completati − già speso);
    - ``opportunity_cap``: costo opportunità vs migliore alternativa residua.

    ``final = min(cap validi)`` intero, mai negativo; ``binding_cap`` = la
    chiave del cap che vincola. Ruolo coperto o squadra non tracciata →
    ``final = None`` (nessun tetto calcolabile). NON si forza ``final >=
    base``: se la squadra non può/sceglie di sostenere il PFC, il max
    sostenibile può stare sotto. La chiave top-level ``maxbid`` == ``final``.

    Espone riserve, allocazione ruolo (target/spent/released/left),
    alternativa residua (pid/nome/prezzo/utility) e candidato
    (utility/risk/status/marginal_gain/urgency)."""
    cfg = auction.cfg
    team = team or cfg["io"]
    tracked = team in auction.state["money"]
    slots = auction.state["slots"].get(team, dict(cfg["slots"]))
    role = p["ruolo"]
    covered = tracked and (
        roster_spots_left(auction, team) <= 0
        if cfg.get("game_mode") == "mantra"
        else slots.get(role, 0) <= 0
    )

    util = fantasy_utility(p)
    risk = STATUS_RISK.get(str(p.get("status") or "").upper(), STATUS_RISK_DEFAULT)
    gain = roster_marginal_gain(auction, p, team)
    suggested = live_price(auction, p)
    reserve = reserve_after_purchase(auction, p, team)
    opp = opportunity_cap(auction, p, team)
    role_info = role_budget_left(auction, team, role)

    base = {
        "role": role,
        "team": team,
        "tracked": tracked,
        "covered": covered,
        "suggested": suggested,
        "reserve": reserve,
        "candidate": {
            "pid": p["pid"],
            "nome": p["nome"],
            "base": p["base"],
            "utility": round(util, 4),
            "risk": risk,
            "status": str(p.get("status") or ""),
            "marginal_gain": round(gain, 4),
            "urgency": opp["urgency"],
        },
        "alternative": opp["alternative"],
        "opportunity": {k: v for k, v in opp.items() if k not in ("alternative",)},
    }
    if not tracked or covered:
        return {
            **base,
            "maxbid": None,
            "final": None,
            "market_cap": None,
            "reserve_cap": None,
            "role_cap": None,
            "opportunity_cap": None,
            "binding_cap": None,
            "role": role_info,
        }

    market_cap = max(0, round(suggested * cfg["aggression"]))
    reserve_cap = max(0, auction.state["money"][team] - reserve["total"])
    role_cap = (
        reserve_cap
        if cfg.get("game_mode") == "mantra"
        else role_info["left"]
        if role_info is not None
        else 0
    )
    caps = {
        "market_cap": market_cap,
        "reserve_cap": reserve_cap,
        "role_cap": role_cap,
        "opportunity_cap": opp["cap"],
    }
    # cap validi = solo quelli numerici (opportunity_cap puo' essere None:
    # nessuna alternativa -> il cap non stringe). ``pairs`` esplicita il tipo
    # (int, str) per una min() omogenea e deterministica (tie-break alfabetico)
    pairs: list[tuple[int, str]] = []
    for k, v in caps.items():
        if v is not None:
            pairs.append((v, k))  # type: ignore[arg-type]  # v: int dopo il filtro
    final = min(pairs)[0] if pairs else None
    binding = min(pairs)[1] if pairs else None
    return {
        **base,
        "maxbid": final,
        "final": final,
        "binding_cap": binding,
        **caps,
        "role": role_info,
    }


# ========================================================================
# WP7 — contratto a tre blocchi: market | fantasy | my_team
# ========================================================================
#
# I tre valori restano DISGIUNTI (nessun indice composito, nessuna media
# ponderata): ``valuation_contract`` li espone insieme per il contratto di
# payload consumato da CLI, API e frontend, ma ogni blocco ha la propria
# semantica e le proprie dipendenze:
#
# - ``market``  : prezzo. Solo dati del listone (PFC/PMA e range) e fattori
#                 live del motore (inflazione, scarsita', sconto invenduto).
#                 ``expected`` e' il ``suggested`` live INVARIATO. Il modello
#                 statistico FM non entra mai qui (gate WP9).
# - ``fantasy`` : rendimento puro non monetario. Utility bounded, indici del
#                 listone, rischio status. Nessun prezzo/budget nel blocco.
# - ``my_team`` : valore in crediti per la rosa. ``team_value`` =
#                 expected x value_ratio x marginal_gain x need, con
#                 ``value_ratio`` = utility del candidato vs migliore
#                 alternativa residua, ``marginal_gain`` = guadagno marginale
#                 sulla rosa gia' acquistata (0 se la rosa e' gia' titolare
#                 migliore), ``need`` = bisogno/urgenza di rosa (buchi
#                 titolari alzano, alternative abbondanti abbassano). Il
#                 rischio status e' GIA' dentro la utility. NIENTE
#                 affordabilita'/budget corrente qui: il budget limita
#                 ``maxbid`` (WP6), non il valore intrinseco per la rosa.
#
# Bounds (tutti espliciti e finiti):
# - ``value_ratio`` in [0.5, 2.0] (stessi clamp del cap opportunita' WP6);
# - ``need`` in [0.6, 1.5]; ``gain_need = clamp(gain x need)`` in [0, 1.5];
# - ``team_value`` = round(expected x value_ratio x gain_need) in [0, 3 x
#   expected] — intero, mai negativo; covered => 0 con reason, untracked =>
#   None.

# Bounds del blocco my_team (riusa i clamp di opportunity_cap per il ratio).
VALUE_RATIO_MIN, VALUE_RATIO_MAX = RATIO_MIN, RATIO_MAX  # 0.5, 2.0
NEED_MIN, NEED_MAX = 0.6, 1.5  # fattore bisogno/urgenza di rosa
GAIN_NEED_MIN, GAIN_NEED_MAX = 0.0, 1.5  # gain x need bounded
RANGE_FALLBACK_FACTOR = 0.2  # ultimo fallback conservativo del range (no dati)


def market_range(auction, p) -> dict:
    """Range di prezzo atteso del giocatore (blocco ``market``).

    Scala il range PFC numerico (``pfc_lo``/``pfc_hi``, WP3) con gli STESSI
    fattori del prezzo atteso (inflazione x scarsita' x sconto invenduto);
    se i numerici mancano o sono incoerenti: fallback sulla semiampiezza
    ``unc_pfc`` attorno a ``expected``; ultimo fallback conservativo
    documentato: ±20% di ``expected`` (senza dati non si inventa precisione).
    Il range contiene SEMPRE ``expected`` (clamp a posteriori, anche quando
    l'arrotondamento scalato uscirebbe) ed e' sempre >= 1. Ritorna
    ``{"lo", "hi", "source"}`` con ``source`` = ``scaled_pfc_range`` |
    ``unc_pfc`` | ``conservative_fallback`` (fonte della stima, trasparente)."""
    cfg = auction.cfg
    infl = auction.inflation()
    scarc = auction.scarcity(p)
    disc = cfg["unsold_discount"] ** auction.state["unsold"].get(p["pid"], 0)
    expected = max(1, round(p["base"] * infl * scarc * disc))
    lo_raw = p.get("pfc_lo")
    hi_raw = p.get("pfc_hi")
    if (
        isinstance(lo_raw, (int, float))
        and isinstance(hi_raw, (int, float))
        and isfinite(lo_raw)
        and isfinite(hi_raw)
        and lo_raw <= hi_raw
    ):
        lo = round(lo_raw * infl * scarc * disc)
        hi = round(hi_raw * infl * scarc * disc)
        source = "scaled_pfc_range"
    else:
        unc = p.get("unc_pfc")
        if isinstance(unc, (int, float)) and isfinite(unc) and unc > 0:
            lo = round(expected - unc)
            hi = round(expected + unc)
            source = "unc_pfc"
        else:
            # ultimo fallback conservativo documentato: senza range ne'
            # incertezza, ±20% attorno al prezzo atteso (nessuna precisione
            # inventata). Lo zero è escluso dal clamp finale (>= 1).
            lo = round(expected * (1 - RANGE_FALLBACK_FACTOR))
            hi = round(expected * (1 + RANGE_FALLBACK_FACTOR))
            source = "conservative_fallback"
    # invarianti del contratto: range >= 1 e contiene sempre expected
    lo = max(1, min(lo, expected))
    hi = max(hi, expected)
    return {"lo": lo, "hi": hi, "source": source}


def market_block(auction, p) -> dict:
    """Blocco ``market`` del contratto WP7: prezzo di mercato atteso e la sua
    incertezza. Solo input di prezzo: PFC/PMA e range (originali e numerici),
    fattori live (inflazione, scarsita', sconto invenduto), fonte incertezza
    (``unc_pfc`` + ``range_source``). ``expected`` e' il ``suggested`` live
    invariato; NESSUN modello statistico e nessuna componente per-singola
    squadra (il mercato non dipende da chi compra)."""
    cfg = auction.cfg
    infl = auction.inflation()
    scarc = auction.scarcity(p)
    disc = cfg["unsold_discount"] ** auction.state["unsold"].get(p["pid"], 0)
    expected = max(1, round(p["base"] * infl * scarc * disc))
    rng = market_range(auction, p)
    return {
        "expected": expected,
        "range": {"lo": rng["lo"], "hi": rng["hi"]},
        "range_source": rng["source"],
        "pfc": p["base"],
        "pfc_range": p.get("pfc_range") or "",
        "pfc_lo": p.get("pfc_lo"),
        "pfc_hi": p.get("pfc_hi"),
        "pma": p.get("pma"),
        "pma_range": p.get("pma_range") or "",
        "pma_lo": p.get("pma_lo"),
        "pma_hi": p.get("pma_hi"),
        "unc_pfc": p.get("unc_pfc"),
        "dpfcpma": p.get("dpfcpma"),
        "infl": infl,
        "scarc": scarc,
        "disc": disc,
        "unsold": auction.state["unsold"].get(p["pid"], 0),
    }


def fantasy_block(p) -> dict:
    """Blocco ``fantasy`` del contratto WP7: rendimento puro NON monetario.

    Utility bounded 0-1 (``fantasy_utility``, con rischio status GIA'
    dentro) + score 0-100 + statistica del listone (expfm, tit, TIX/FIX,
    fix_contrib, rigori/calci piazzati, status/risk, slot/fascia). Nessun
    prezzo/budget dentro questo blocco per contratto (testato)."""
    util = fantasy_utility(p)
    risk = STATUS_RISK.get(str(p.get("status") or "").upper(), STATUS_RISK_DEFAULT)
    return {
        "utility": round(util, 4),
        "score": round(util * 100),
        "expfm": p.get("expfm"),
        "tit": p.get("tit"),
        "tix": p.get("tix"),
        "fix": p.get("fix"),
        "fix_contrib": p.get("fix_contrib"),
        "pen_prob": p.get("pen_prob"),  # rigori
        "fk_prob": p.get("fk_prob"),  # calci piazzati
        "status": str(p.get("status") or ""),
        "risk": risk,
        "slot": p.get("slot"),
        "fascia": p.get("fascia") or "",
    }


def team_value_breakdown(auction, p, team=None) -> dict:
    """Valore per la rosa (blocco ``my_team``), separato dal maxbid.

    ``team_value = round(expected x value_ratio x marginal_gain x need)``:
    - ``expected``: prezzo di mercato atteso (``live_price``, mai il modello
      statistico);
    - ``value_ratio = clamp(utility(p) / utility(migliore alternativa))`` in
      [0.5, 2.0]; nessuna alternativa residua => 1.0 (neutro);
    - ``marginal_gain``: ``roster_marginal_gain`` (0-1) — guadagno sulla rosa
      gia' acquistata (0 se la rosa e' gia' titolare migliore);
    - ``need``: bisogno/urgenza di rosa = clamp(1 + 0.5 x holes_ratio - 0.1 x
      n_alt) in [0.6, 1.5] — buchi titolari alzano, alternative abbondanti
      abbassano (puoi aspettare).

    Dipende da rosa/slot/alternative ma NON da affordabilita' o budget
    corrente: il budget limita ``maxbid`` (WP6), non il valore intrinseco per
    la rosa (testato: un top-up di budget cambia maxbid, mai team_value).
    Squadra non tracciata => ``team_value = None`` (reason ``untracked``);
    ruolo coperto => ``team_value = 0`` (reason ``covered``). Intero, mai
    negativo, bounded in [0, 3 x expected]. Ritorna un breakdown esplicito
    (formula, ratio, gain, need, holes_ratio, n_alt, alternativa residua)."""
    cfg = auction.cfg
    team = team or cfg["io"]
    tracked = team in auction.state["money"]
    slots = auction.state["slots"].get(team, dict(cfg["slots"]))
    role = p["ruolo"]
    covered = tracked and (
        roster_spots_left(auction, team) <= 0
        if cfg.get("game_mode") == "mantra"
        else slots.get(role, 0) <= 0
    )

    expected = live_price(auction, p)
    util = fantasy_utility(p)
    alt = best_alternative(auction, p, team)
    if alt is None:
        ratio = 1.0
        alt_util = None
    else:
        alt_util = fantasy_utility(alt)
        ratio = _bounded(util / max(alt_util, 1e-9), VALUE_RATIO_MIN, VALUE_RATIO_MAX)
    gain = roster_marginal_gain(auction, p, team)

    mantra = None
    if cfg.get("game_mode") == "mantra":
        mantra = tactical_impact(auction, p, team, fantasy_utility)
        holes_ratio = _bounded(len(mantra["holes_before"]) / 11, 0.0, 1.0)
        n_alt = sum(
            1
            for k in auction.state["pool"]
            if k != p["pid"] and auction._same_job(p, auction.players[k])
        )
    else:
        formation = cfg["formation"][role]
        filled = _purchased_in_role(auction, team, role)
        holes_ratio = _bounded(max(0, formation - filled) / max(formation, 1), 0.0, 1.0)
        n_alt = sum(
            1
            for k in auction.state["pool"]
            if k != p["pid"] and auction.players[k]["ruolo"] == role
        )
    need = _bounded(
        1.0 + 0.5 * holes_ratio - ALT_ABUNDANCE_STEP * n_alt, NEED_MIN, NEED_MAX
    )
    gain_need = _bounded(gain * need, GAIN_NEED_MIN, GAIN_NEED_MAX)
    alt_utility = alt_util if isinstance(alt_util, (int, float)) else 0.0

    base = {
        "role": role,
        "team": team,
        "tracked": tracked,
        "covered": covered,
        "expected": expected,
        "formula": "expected × value_ratio × marginal_gain × need",
        "value_ratio": round(ratio, 4),
        "marginal_gain": round(gain, 4),
        "need": round(need, 4),
        "gain_need": round(gain_need, 4),
        "holes_ratio": round(holes_ratio, 4),
        "n_alt": n_alt,
        "alternative": (
            {
                "pid": alt["pid"],
                "nome": alt["nome"],
                "utility": round(alt_utility, 4),
            }
            if alt is not None
            else None
        ),
    }
    if mantra is not None:
        base["mantra"] = mantra
    if not tracked:
        return {**base, "team_value": None, "reason": "untracked"}
    if covered:
        return {**base, "team_value": 0, "reason": "covered"}
    value = round(expected * ratio * gain_need)
    return {**base, "team_value": value, "reason": None}


def valuation_contract(auction, p, team=None) -> dict:
    """Contratto di valutazione WP7: ``{"market", "fantasy", "my_team"}``.

    API PURA (nessuna ricorsione su ``Auction.evaluate``: usa solo fattori e
    funzioni del modulo) che espone i TRE valori disgiunti del giocatore per
    la squadra ``team`` (default ``cfg["io"]``), senza indice unico:

    - ``market``  = prezzo di mercato atteso e incertezza (``market_block``);
    - ``fantasy`` = rendimento puro non monetario (``fantasy_block``);
    - ``my_team`` = valore per la rosa in crediti (``team_value_breakdown``).

    ``Auction.evaluate`` espone questi tre blocchi additivi mantenendo tutte
    le chiavi legacy; ``player_payload`` (web) li propaga al frontend. I
    numeri ``suggested``/``maxbid`` restano identici al pre-WP7."""
    team = team or auction.cfg["io"]
    return {
        "market": market_block(auction, p),
        "fantasy": fantasy_block(p),
        "my_team": team_value_breakdown(auction, p, team),
    }
