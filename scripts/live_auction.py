#!/usr/bin/env -S uv run --quiet python
"""
Asta live — Fantacalcio Stagione 2026/27 (Listone Fantaculo).

Prezzo d'asta dinamico basato sul Listone Fantaculo (data/listone.csv, generato da
scripts/import_listone.py), con valutazione dell'inflazione in tempo reale:

1. Valore base     — il PFC (prezzo suggerito Fantaculo, punto medio del range). Il range
                     resta come riferimento: dentro il range si e' "in acque tranquille".
2. Inflazione      — indice I = (crediti residui / valore residuo in carta) normalizzato a 1
                     all'inizio. La somma dei PFC del listone supera il budget della lega:
                     se le stelle vengono pagate ai PFC (o sopra), i crediti residui scendono
                     piu' in fretta del valore residuo -> il resto del listone si sconta.
                     Il valore in carta conta solo i giocatori con PFC >= value_floor
                     (la coda nominale da 1-2 crediti non e' mercato).
3. Scarsità       — moltiplicatore per giocatore con due componenti:
                     a) slot: rapporto tra slot ancora aperti nel ruolo e alternative
                     rimaste con slot consigliato <= al suo (giocatori che fanno lo
                     stesso lavoro), confrontato con il rapporto iniziale;
                     b) valore: PFC delle alternative rimaste vs il suo — a parita' di
                     fascia, quando escono gli alternative costosi sale di piu' quello
                     costoso (l'ultimo pezzo di qualita' vale di piu'). Se la qualita'
                     si consuma piu' veloce dei slot -> premio; viceversa sconto.
                     Slot 1 "si schiera sempre", 2 "90% titolare", 3 "si gestisce",
                     4 "si evita", 5+ fuori lista.
4. Indici giocatore — TIX: indice titolarita' (percentile nel ruolo dell'expected titolarita');
                     FIX: indice FM attesa (percentile nel ruolo del contributo bonus a
                     giornata = (FM attesa - 6) x titolarita'/100).
5. Qualità residua  — per ogni ruolo: quanta della qualita' utile originaria (punti di
                     titolarita' sopra soglia) e' ancora in carta. Scende quando escono
                     i titolari utili, in proporzione alla loro titolarita'.
6. Prezzo live      = valore base x inflazione x scarsità (x eventuale sconto invenduto),
                     con tetto dato dal budget reale della propria squadra.

Uso:
  uv run scripts/live_auction.py                        # sessione live interattiva
  uv run scripts/live_auction.py --prezzo "Dimarco"     # valutazione singola
  uv run scripts/live_auction.py --demo                 # simulazione dimostrativa
"""

import argparse
import copy
import csv
import math
import os
import shlex
import sys
from collections import defaultdict
from typing import Any

from import_listone import (
    compute_pid,
    is_pid,
    parse_range,
)
from league_config import (
    DEFAULTS,
    ROLE_LABEL,
    ROLE_ORDER,
    feasibility,
    norm,
    normalize,
    validate,
)
from mantra import (  # pyright: ignore[reportMissingImports]
    compatible_positions,
    has_explicit_roles,
    player_roles,
    roles_text,
    roster_players,
    roster_spots_left,
    same_job,
)
from valuation import (
    max_bid_breakdown,
    scarcity_breakdown,
    valuation_contract,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(BASE_DIR, "data", "listone.csv")

ROLE_SING = {
    "P": "Portiere",
    "D": "Difensore",
    "C": "Centrocampista",
    "A": "Attaccante",
}

SLOT_LABEL = {1: "titolare sempre", 2: "90% titolare", 3: "da gestire", 4: "da evitare"}
STATUS_LABEL = {"T": "titolare", "B": "ballottaggio", "P": "panchina"}


def _require_positive_float(r, key, ctx):
    """Colonna OBBLIGATORIA (es. pfc): numero > 0, errore contestualizzato se
    assente, vuota o non numerica. Niente fallback silenziosi sui numeri
    obbligatori malformati."""
    raw = r.get(key)
    if raw is None or str(raw).strip() == "":
        raise ValueError(f"{ctx}: colonna '{key}' mancante o vuota")
    try:
        v = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{ctx}: colonna '{key}' non numerica: {raw!r}") from None
    if not (v > 0):
        raise ValueError(f"{ctx}: colonna '{key}' deve essere > 0 (trovato {raw!r})")
    return v


def _opt_float(r, key, ctx, default):
    """Colonna opzionale: assente/vuota -> default (backward-compat); presente ma
    malformata -> errore contestualizzato (mai fallback silenziosi)."""
    raw = r.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"{ctx}: colonna '{key}' non numerica: {raw!r}") from None


def _opt_int(r, key, ctx, default):
    raw = r.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip().replace(",", ".")))
    except (TypeError, ValueError):
        raise ValueError(f"{ctx}: colonna '{key}' non intera: {raw!r}") from None


def _opt_float_none(r, key, ctx):
    """Range/incertezza numerici (pfc_lo, unc_pfc, ...): advisory, non inventati.
    Assenti o malformati -> None (nessun dato inventato)."""
    raw = r.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _load_mantra_sidecar(path):
    """Ruoli ufficiali opzionali da ``mantra_roles.csv`` accanto al listone.

    Il join usa il nome normalizzato, non la squadra: i ruoli sopravvivono ai
    trasferimenti e il PID resta indipendente dagli aggiornamenti Mantra.
    """
    sidecar = os.path.join(os.path.dirname(os.path.abspath(path)), "mantra_roles.csv")
    try:
        with open(sidecar, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return {}
    return {
        norm(row.get("nome") or ""): row.get("ruolo_mantra") or ""
        for row in rows
        if norm(row.get("nome") or "") and str(row.get("ruolo_mantra") or "").strip()
    }


def load_players(path):
    """Legge il CSV canonico del listone in liste di dict giocatore (schema WP3).

    Backward-compatible con CSV vecchi senza ``pid``/range numerici: il pid viene
    derivato con lo stesso algoritmo dell'import (compute_pid) e i range numerici
    vengono parse-dai range stringa quando le colonne dedicate mancano. I numeri
    OBBLIGATORI (pfc) malformati, e i numeri presenti ma malformati, alzano un
    ValueError contestualizzato (niente fallback silenziosi). Ogni giocatore esce
    sempre con ``pid`` (colonna o derivato)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError as e:
        raise FileNotFoundError(f"CSV del listone non trovato: {path}") from e
    mantra_sidecar = _load_mantra_sidecar(path)
    players = []
    for i, r in enumerate(rows):
        role = (r.get("ruolo") or "").strip().upper()
        if role not in ROLE_ORDER:
            continue
        nome = (r.get("nome") or "").strip()
        ctx = f"riga {i + 2} ({nome or '?'}, {role})"
        pfc = _require_positive_float(r, "pfc", ctx)
        pma = _opt_float(r, "pma", ctx, pfc)
        slot = _opt_int(r, "slot", ctx, 8)
        tit = _opt_float(r, "tit", ctx, 0.0)
        expfm = _opt_float(r, "expfm", ctx, 6.0)

        pfc_range = r.get("pfc_range", "") or ""
        pma_range = r.get("pma_range", "") or ""
        # range numerici: colonne dedicate se presenti, altrimenti parse dai range
        pfc_lo = _opt_float_none(r, "pfc_lo", ctx)
        pfc_hi = _opt_float_none(r, "pfc_hi", ctx)
        pma_lo = _opt_float_none(r, "pma_lo", ctx)
        pma_hi = _opt_float_none(r, "pma_hi", ctx)
        if pfc_lo is None or pfc_hi is None:
            rr = parse_range(pfc_range)
            if rr:
                pfc_lo, pfc_hi = rr
        if pma_lo is None or pma_hi is None:
            rr = parse_range(pma_range)
            if rr:
                pma_lo, pma_hi = rr
        unc_pfc = _opt_float_none(r, "unc_pfc", ctx)
        if unc_pfc is None and pfc_lo is not None and pfc_hi is not None:
            unc_pfc = round((pfc_hi - pfc_lo) / 2, 1)

        # identita': pid dalla colonna (validato) o derivato deterministicamente
        pid = (r.get("pid") or "").strip()
        if pid:
            if not is_pid(pid):
                raise ValueError(f"{ctx}: pid malformato (attesi 16 hex): {pid!r}")
        else:
            pid = compute_pid(nome, role)

        mantra_raw = (
            r.get("ruolo_mantra")
            or r.get("roleMantra")
            or mantra_sidecar.get(norm(nome))
            or ""
        )
        mantra_source = {"ruolo": role, "ruolo_mantra": mantra_raw}
        players.append(
            {
                "pid": pid,
                "nome": nome,
                "squadra": r.get("squadra", "") or "",
                "ruolo": role,
                "ruolo_mantra": roles_text(mantra_source),
                "ruoli_mantra": list(player_roles(mantra_source)),
                "mantra_roles_explicit": bool(str(mantra_raw).strip()),
                "pfc": pfc,
                "pma": pma,
                "pfc_range": pfc_range.strip(),
                "pma_range": pma_range.strip(),
                "pfc_lo": pfc_lo,
                "pfc_hi": pfc_hi,
                "pma_lo": pma_lo,
                "pma_hi": pma_hi,
                "unc_pfc": unc_pfc,
                "dpfcpma": _opt_float(r, "dpfcpma", ctx, round(pfc - pma, 1)),
                "slot": slot,
                "tit": tit,
                "expfm": expfm,
                "fascia": r.get("fascia", "") or "",
                "status": (r.get("status") or "").strip(),
                "pen_prob": _opt_float(r, "pen_prob", ctx, 0.0),
                "fk_prob": _opt_float(r, "fk_prob", ctx, 0.0),
                "tix": _opt_float(r, "tix", ctx, 50.0),
                "fix": _opt_float(r, "fix", ctx, 50.0),
                "fix_contrib": _opt_float(r, "fix_contrib", ctx, 0.0),
            }
        )
    return players


class AmbiguousName(Exception):
    pass


class AuctionError(Exception):
    """Errore di dominio dell'asta: e' stata violata una regola della vendita."""


class NotInPoolError(AuctionError):
    """Il giocatore non e' (piu') nel pool: niente doppie vendite ne' invenduti su venduti."""


class InvalidPriceError(AuctionError):
    """Il prezzo non e' un intero positivo."""


class InvalidTeamError(AuctionError):
    """La squadra non e' tracciata: servono un nome valido oppure 'ALTRO' esplicito."""


class InsufficientBudgetError(AuctionError):
    """La squadra non ha crediti sufficienti: il budget non puo' andare negativo."""


class SlotUnavailableError(AuctionError):
    """La squadra ha gia' esaurito gli slot del ruolo: niente slot negativi."""


class ConfigError(Exception):
    """La configurazione della lega non e' valida o non e' sostenibile dal pool."""


class Auction:
    def __init__(self, players, **overrides):
        # La config effettiva e' normalizzata dallo schema unico di league_config
        # (defaults, nomi squadra, slot/formation per ruolo) e validata qui:
        # strutturale + fattibilita' rispetto ai player. Il profilo "engine"
        # (teams >= 1, budget >= 1, pool parziale ammessi per simulazioni; le
        # invarianti strutturali — come formation <= slot — valgono identiche al
        # profilo pubblico) e' quello del costruttore interno; le entry point
        # (validate_config, CLI, web) applicano SEMPRE il profilo pubblico prima
        # di costruire il motore.
        self.cfg = normalize(overrides)
        errors = list(validate(self.cfg, profile="engine"))
        errors += feasibility(self.cfg, players, profile="engine").errors
        if errors:
            raise ConfigError("; ".join(errors))
        # deep-copy di ogni dict player: il chiamante resta proprietario dei suoi
        # oggetti, l'Auction non condivide mai memoria con lui ('base' viene aggiunto
        # ai giocatori INTERNI, mai a quelli del chiamante). L'identita' e' il ``pid``:
        # chiave di players e di pool/sold/unsold. Ogni player ha SEMPRE un pid
        # (dalla riga CSV o derivato con lo stesso algoritmo dell'import); un pid
        # duplicato (omonimi reali nello stesso ruolo) e' un errore bloccante, mai
        # un'unione automatica.
        self.players = {}
        for p in players:
            d = copy.deepcopy(p)
            pid = d.get("pid") or compute_pid(d["nome"], d["ruolo"])
            d["pid"] = pid
            explicit_roles = has_explicit_roles(d)
            d["ruoli_mantra"] = list(player_roles(d))
            d["ruolo_mantra"] = roles_text(d)
            d["mantra_roles_explicit"] = explicit_roles
            prev = self.players.get(pid)
            if prev is not None:
                raise ConfigError(
                    f"collisione di identita' (pid {pid}): {prev['nome']} e {d['nome']} "
                    f"condividono nome normalizzato e ruolo — identita' ambigua, "
                    f"non si uniscono automaticamente"
                )
            self.players[pid] = d
        for p in self.players.values():
            p["base"] = max(1, round(p["pfc"]))
        self.classic_role_index: dict[str, set[str]] = defaultdict(set)
        self.mantra_role_index: dict[str, set[str]] = defaultdict(set)
        self.player_search_index: dict[str, str] = {}
        for pid, player in self.players.items():
            self.player_search_index[pid] = (
                f"{norm(player['nome'])} {norm(player.get('squadra') or '')}"
            )
            self.classic_role_index[player["ruolo"]].add(pid)
            for role in player_roles(player):
                self.mantra_role_index[role].add(pid)
        self._init_state()

    def _init_state(self):
        cfg = self.cfg
        names = (
            list(cfg["team_names"])
            if cfg.get("team_names")
            else [cfg["io"]] + [f"T{i}" for i in range(1, cfg["teams"])]
        )
        if len(names) != cfg["teams"]:
            raise ValueError(f"attese {cfg['teams']} squadre, trovate {len(names)}")
        if cfg["io"] not in names:
            names[0] = cfg["io"]
        cfg["team_names"] = names
        self.state = {
            "money_league": cfg["teams"] * cfg["budget"],
            "money": dict.fromkeys(names, cfg["budget"]),
            "slots": {t: dict(cfg["slots"]) for t in names},
            "pool": set(self.players),
            "sold": [],
            "spent_unknown": 0,
            "unsold": defaultdict(int),
        }
        self.undo_stack = []
        self.demand0 = {role: cfg["teams"] * cfg["slots"][role] for role in ROLE_ORDER}
        self.mantra_demand0 = {
            k: cfg["teams"]
            * max(1, len(compatible_positions(p, cfg["mantra_formation"])))
            for k, p in self.players.items()
        }
        self.alt0 = {k: self._comparable_count(p) for k, p in self.players.items()}
        self.alt_value0 = {
            k: self._alt_value(p, pool=set(self.players))
            for k, p in self.players.items()
        }
        self.total_money0 = cfg["teams"] * cfg["budget"]
        self.total_value0 = self._value_rem(pool=None)
        self.total_value0_pma = self._value_rem_pma(pool=None)
        self.role_pool0 = {
            role: sum(p["base"] for p in self.players.values() if p["ruolo"] == role)
            for role in ROLE_ORDER
        }
        self.quality0 = {
            role: sum(
                max(0, p["tit"] - cfg["tit_cov_threshold"])
                for p in self.players.values()
                if p["ruolo"] == role
            )
            for role in ROLE_ORDER
        }

    @classmethod
    def validate_config(cls, players, cfg):
        """Wrapper compatibile che delega la validazione a league_config (profilo pubblico).

        Unico punto di verita' per CLI e API: errori strutturali (teams/budget nei
        limiti 2-20/10-10000, slot e formazione per ruolo, formazione <= slot, nomi
        unici con 'io' presente) PIU' fattibilita' rispetto al listone (per ogni ruolo
        abbastanza giocatori per teams x slots[ruolo], budget >= costo minimo
        completamento rosa). Alza ConfigError con la lista errori; non muta nulla
        (lo stato resta quello della lega corrente).
        """
        norm_cfg = normalize(cfg)
        errors = list(validate(norm_cfg))
        errors += feasibility(norm_cfg, players).errors
        if errors:
            raise ConfigError("; ".join(errors))
        return norm_cfg

    def _snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.state))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)

    # ------------------------------------------------------------- primitives
    def player_ids_for_role(self, role: str) -> set[str]:
        """Immutable-at-runtime role lookup intersected with the live pool by callers."""
        index = (
            self.mantra_role_index
            if self.cfg.get("game_mode") == "mantra"
            else self.classic_role_index
        )
        return index.get(role, set())

    def _same_job(self, candidate, other):
        if self.cfg.get("game_mode") == "mantra":
            return same_job(candidate, other, self.cfg["mantra_formation"])
        return other["ruolo"] == candidate["ruolo"]

    def _comparable_count(self, p):
        """Alternative di qualità pari o migliore che possono coprire lo stesso lavoro."""
        return sum(
            1
            for k in self.state["pool"]
            if self._same_job(p, self.players[k])
            and self.players[k]["slot"] <= p["slot"]
        )

    def _alt_value(self, p, pool=None):
        """Valore (PFC) delle alternative rimaste, escluso il giocatore stesso."""
        pool = self.state["pool"] if pool is None else pool
        key = p["pid"]
        return sum(
            self.players[k]["base"]
            for k in pool
            if k != key
            and self._same_job(p, self.players[k])
            and self.players[k]["slot"] <= p["slot"]
        )

    def _player_demand(self, p):
        if self.cfg.get("game_mode") != "mantra":
            return self._demand(p["ruolo"])
        sold_count = sum(
            1
            for pid, _price, _team, _role in self.state["sold"]
            if pid in self.players and self._same_job(p, self.players[pid])
        )
        return max(0, self.mantra_demand0[p["pid"]] - sold_count)

    def _demand(self, role):
        """Slot ancora aperti nel ruolo, mai negativi: le vendite ALTRO possono
        superare il tetto iniziale, quindi la domanda residua resta bounded a 0."""
        sold_count = sum(1 for _, _, _, r in self.state["sold"] if r == role)
        return max(0, self.demand0[role] - sold_count)

    def _value_rem(self, pool=None):
        floor = self.cfg["value_floor"]
        pool = self.state["pool"] if pool is None else pool
        return sum(
            self.players[k]["base"] for k in pool if self.players[k]["base"] >= floor
        )

    def _value_rem_pma(self, pool=None):
        """Valore residuo stimato ai prezzi medi d'Italia (PMA), stessi giocatori del libro PFC."""
        floor = self.cfg["value_floor"]
        pool = self.state["pool"] if pool is None else pool
        return sum(
            self.players[k]["pma"] for k in pool if self.players[k]["base"] >= floor
        )

    def inflation(self):
        """Indice vs PFC: crediti residui / valore residuo ai prezzi suggeriti.
        Mai negativo: i crediti residui sono clampati a 0 anche sotto stato anomalo."""
        value_rem = self._value_rem()
        money_rem = max(self.state["money_league"], 0)
        if value_rem <= 0:
            return 1.0
        return (money_rem / value_rem) / (self.total_money0 / max(self.total_value0, 1))

    def inflation_pma(self):
        """Indice vs PMA: crediti residui / valore residuo ai prezzi medi d'Italia.
        Mai negativo: i crediti residui sono clampati a 0 anche sotto stato anomalo."""
        value_rem = self._value_rem_pma()
        money_rem = max(self.state["money_league"], 0)
        if value_rem <= 0:
            return 1.0
        return (money_rem / value_rem) / (
            self.total_money0 / max(self.total_value0_pma, 1)
        )

    def scarcity(self, p):
        """Moltiplicatore di scarsita' = componente slot x componente valore.

        - slot:  slot aperti nel ruolo vs alternative di pari fascia (slot <= suo),
          rispetto alla situazione iniziale.
        - valore: PFC delle alternative rimaste vs il suo, rispetto all'inizio.
          A parita' di fascia, l'uscita degli alternative piu' costosi carezza
          di piu' il giocatore costoso (l'ultimo pezzo di qualita' vale di piu').
        """
        cfg = self.cfg
        demand = self._player_demand(p)
        alt = max(self._comparable_count(p), 1)
        alt0 = max(self.alt0[p["pid"]], 1)
        demand0 = max(
            self.mantra_demand0[p["pid"]]
            if cfg.get("game_mode") == "mantra"
            else self.demand0[p["ruolo"]],
            1,
        )
        s_slot = ((demand / alt) / (demand0 / alt0)) ** cfg["scarcity_beta"]
        s_slot = max(cfg["scarcity_min"], min(cfg["scarcity_max"], s_slot))

        alt_value = max(self._alt_value(p), 1)
        alt_value0 = max(self.alt_value0[p["pid"]], 1)
        s_value = (alt_value0 / alt_value) ** cfg["scarcity_value_beta"]
        s_value = max(
            cfg["scarcity_value_min"], min(cfg["scarcity_value_max"], s_value)
        )

        factor = s_slot * s_value
        return max(cfg["scarcity_min"], min(cfg["scarcity_max"], factor))

    def quality_left(self, role):
        """Qualita' residua del ruolo: quanta della qualita' utile originaria (punti di
        titolarita' sopra la soglia) e' ancora in carta. 100% = situazione iniziale.
        Vendere un top la fa scendere in proporzione alla sua titolarita'; vendere
        panchinari (tit < soglia) non la tocca. Monotona: puo' solo scendere."""
        cfg = self.cfg
        w_rem = sum(
            max(0, self.players[k]["tit"] - cfg["tit_cov_threshold"])
            for k in self.state["pool"]
            if self.players[k]["ruolo"] == role
        )
        return round(100 * w_rem / max(self.quality0[role], 1))

    def starters_left(self, role):
        cfg = self.cfg
        return sum(
            1
            for k in self.state["pool"]
            if self.players[k]["ruolo"] == role
            and self.players[k]["slot"] <= cfg["starter_slot_max"]
        )

    def fix_mean(self, role):
        """Contributo bonus atteso medio dei giocatori ancora copribili (tit >= soglia)."""
        cfg = self.cfg
        vals = [
            self.players[k]["fix_contrib"]
            for k in self.state["pool"]
            if self.players[k]["ruolo"] == role
            and self.players[k]["tit"] >= cfg["tit_cov_threshold"]
        ]
        return sum(vals) / len(vals) if vals else 0.0

    # ---------------------------------------------------------------- actions
    def _pid_for_name(self, key):
        """pid dei giocatori il cui nome normalizzato == ``key`` (0/1/molti)."""
        return [pid for pid, p in self.players.items() if norm(p["nome"]) == key]

    def resolve(self, text):
        """pid del giocatore per pid esatto o nome normalizzato (match esatto poi
        substring). Ritorna None se assente; alza AmbiguousName se il nome e'
        ambiguo (omonimi — stesso nome normalizzato con ruoli/squadre diversi).
        Il canale piu' importante per API e CLI: accetta sia il pid sia il nome
        legacy, con AmbiguousName quando necessario."""
        t = str(text).strip()
        if t in self.players:  # pid esatto
            return t
        key = norm(t)
        exact = self._pid_for_name(key)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            top = sorted(exact, key=lambda pid: -self.players[pid]["base"])[:8]
            raise AmbiguousName(", ".join(self.players[pid]["nome"] for pid in top))
        matches = [
            pid
            for pid in self.players
            if key and key in norm(self.players[pid]["nome"])
        ]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        top = sorted(matches, key=lambda pid: -self.players[pid]["base"])[:8]
        raise AmbiguousName(", ".join(self.players[pid]["nome"] for pid in top))

    def find(self, text):
        """Lookup compatibile: pid esatto o nome (vedi ``resolve``). Ritorna il dict
        giocatore o None; AmbiguousName per omonimi. Tutte le funzioni CLI/web
        usano questo canale (cerca ancora per nome)."""
        pid = self.resolve(text)
        return self.players[pid] if pid else None

    def evaluate(self, p, team=None) -> dict[str, Any]:
        cfg = self.cfg
        team = team or cfg["io"]
        if p["pid"] not in self.state["pool"]:
            return {"error": f"{p['nome']} e' gia' stato venduto."}
        # WP7: contratto esplicito a tre blocchi disgiunti (market | fantasy |
        # my_team) — API pura senza ricorsione; ``market["expected"]`` ==
        # ``suggested`` live invariato, ``my_team["team_value"]`` separato dal
        # maxbid sostenibile (WP6). Tutte le chiavi legacy restano invariate.
        contract = valuation_contract(self, p, team)
        mkt = contract["market"]
        bd = scarcity_breakdown(self, p, team)
        mb = max_bid_breakdown(self, p, team)
        base = p["base"]
        slots = self.state["slots"].get(team, dict(cfg["slots"]))
        money = self.state["money"].get(team)
        tracked = money is not None
        role = p["ruolo"]
        is_mantra = cfg.get("game_mode") == "mantra"
        spots_left = roster_spots_left(self, team) if is_mantra else sum(slots.values())
        covered = spots_left <= 0 if is_mantra else slots[role] <= 0
        role_slots = (
            len(compatible_positions(p, cfg["mantra_formation"]))
            if is_mantra
            else slots[role]
        )
        return {
            "base": base,
            "infl": mkt["infl"],
            "scarc": mkt["scarc"],
            "disc": mkt["disc"],
            "suggested": mkt["expected"],
            "maxbid": mb["final"],
            "covered": covered,
            "tracked": tracked,
            "alt": self._comparable_count(p),
            "alt_value": self._alt_value(p),
            "demand": self._player_demand(p),
            "money": money,
            "slots_role": role_slots,
            "slots_left": spots_left,
            "mantra_roles": list(player_roles(p)) if is_mantra else [],
            "mantra_formation": cfg.get("mantra_formation") if is_mantra else None,
            "scarcity_team": bd["final"],
            "scarcity_breakdown": bd,
            "maxbid_breakdown": mb,
            # WP7: i tre blocchi additivi del contratto (nessun indice unico)
            "market": contract["market"],
            "fantasy": contract["fantasy"],
            "my_team": contract["my_team"],
        }

    def _resolve_team(self, team):
        """None o 'ALTRO' (case-insensitive) -> 'ALTRO'; altrimenti deve corrispondere
        a una squadra tracciata (confronto normalizzato), restituita in forma canonica."""
        if team is None or norm(team) == "altro":
            return "ALTRO"
        for name in self.state["money"]:
            if norm(name) == norm(team):
                return name
        raise InvalidTeamError(
            f"squadra non valida: {team!r} — servono una squadra tracciata "
            f"oppure 'ALTRO' esplicito"
        )

    def _validate_sale(self, p, price, team):
        """Invarianti di vendita, centralizzati qui (CLI e web passano da mark_sold)."""
        if p["pid"] not in self.state["pool"]:
            raise NotInPoolError(
                f"{p['nome']} e' gia' stato venduto: doppie vendite vietate."
            )
        if type(price) is not int or price < 1:
            raise InvalidPriceError(
                f"prezzo non valido: {price!r} (serve un intero positivo)"
            )
        team = self._resolve_team(team)
        if team != "ALTRO":  # squadra tracciata: budget sufficiente e slot libero
            if price > self.state["money"][team]:
                raise InsufficientBudgetError(
                    f"{team} ha {self.state['money'][team]} cr, il prezzo e {price} cr: "
                    f"budget insufficiente (il budget non puo' andare negativo)."
                )
            if price > self.state["money_league"]:
                # anche una vendita tracciata esce dal totale iniziale della lega:
                # se ALTRO ha gia' assorbito i crediti, nessuna squadra puo' spingere
                # money_league in rosso (invariante ledger: mai negativo)
                raise InsufficientBudgetError(
                    f"crediti residui della lega ({self.state['money_league']} cr) "
                    f"inferiori al prezzo di {price} cr: il totale della lega "
                    f"non puo' andare negativo."
                )
            if self.cfg.get("game_mode") == "mantra":
                roster = roster_players(self, team)
                spots = roster_spots_left(self, team)
                if spots <= 0:
                    raise SlotUnavailableError(
                        f"{team} ha completato la rosa Mantra da {self.cfg['roster_size']} giocatori."
                    )
                roles = player_roles(p)
                if not roles or not has_explicit_roles(p):
                    raise SlotUnavailableError(
                        f"{p['nome']} non ha ruoli Mantra ufficiali nel listone."
                    )
                keepers = sum(1 for q in roster if "Por" in player_roles(q))
                is_keeper = "Por" in roles
                if is_keeper and keepers >= 15:
                    raise SlotUnavailableError(
                        f"{team} ha gia' il massimo di 15 portieri Mantra."
                    )
                if not is_keeper and len(roster) - keepers >= 75:
                    raise SlotUnavailableError(
                        f"{team} ha gia' il massimo di 75 giocatori di movimento Mantra."
                    )
                keepers_after = keepers + (1 if is_keeper else 0)
                if spots - 1 < max(0, 2 - keepers_after):
                    raise SlotUnavailableError(
                        f"acquisto impossibile: {team} deve conservare posto per almeno 2 portieri Mantra."
                    )
            elif self.state["slots"][team][p["ruolo"]] <= 0:
                raise SlotUnavailableError(
                    f"{team} ha esaurito gli slot di {ROLE_SING[p['ruolo']]}: "
                    f"slot rimasti {self.state['slots'][team][p['ruolo']]} "
                    f"(slot negativi vietati)."
                )
        elif price > self.state["money_league"]:  # ALTRO attinge ai crediti della lega
            raise InsufficientBudgetError(
                f"ALTRO spende dai crediti residui della lega "
                f"({self.state['money_league']} cr): il prezzo di {price} cr la "
                f"porterebbe in rosso, vietato."
            )
        return team

    def mark_sold(self, p, price, team, eval_before=None):
        """Registra l'acquisto validando le regole d'asta; solleva AuctionError se violata.

        Restituisce la squadra canonicamente risolta (una squadra tracciata con
        il nome corretto dalla mappa, oppure 'ALTRO'), cosi' eventi e report
        restano allineati a state['sold'].

        ``eval_before`` (opzionale, adattatore WP4) e' la valutazione del
        giocatore PRIMA della vendita: ignorata qui (e' un dato di evento che
        ``TrendAuction._record`` usa per il premio vs modello). Il replay
        dell'event-log (scripts/auction_store.apply_event) la passa in modo
        uniforme anche a questo costruttore puro.
        """
        team = self._validate_sale(p, price, team)
        self._snapshot()
        key = p["pid"]
        self.state["pool"].discard(key)
        # lo sconto invenduto vale solo finche' il giocatore e' nel pool:
        # alla vendita il contatore decade (invariante "unsold solo nel pool")
        self.state["unsold"].pop(key, None)
        self.state["money_league"] -= price
        if team != "ALTRO":  # squadra tracciata: aggiorna budget e slot (mai negativi)
            self.state["money"][team] -= price
            if self.cfg.get("game_mode") != "mantra":
                self.state["slots"][team][p["ruolo"]] -= 1
        else:  # acquirente non tracciato: contabilizza solo il denaro uscito dalla lega
            self.state["spent_unknown"] += price
        self.state["sold"].append((key, price, team, p["ruolo"]))
        return team

    def mark_unsold(self, p):
        """Registra l'invenduto: solo per giocatori ancora nel pool."""
        if p["pid"] not in self.state["pool"]:
            raise NotInPoolError(
                f"{p['nome']} non e' nel pool: l'invenduto si registra solo su "
                f"giocatori ancora in asta."
            )
        self._snapshot()
        self.state["unsold"][p["pid"]] += 1

    def undo(self):
        if not self.undo_stack:
            return False
        self.state = self.undo_stack.pop()
        return True

    # --------------------------------------------------------------- invariants
    def check_invariants(self) -> list[str]:
        """Verifica le invarianti di stato; ritorna le violazioni (lista vuota = ok).

        Non muta lo stato. Copre:
        1. Ledger monetario: con la semantica ALTRO attuale i crediti della lega
           sono `money_league == sum(money.values()) - spent_unknown` (le vendite
           ALTRO escono dal denaro lega senza toccare le squadre tracciate e
           vengono contabilizzate in spent_unknown), che deve equivalere a
           `total_money0 - somma prezzi venduti`; tutto mai negativo, e
           spent_unknown == somma delle sole vendite ALTRO.
        2. Pool/sold/slot: pool ⊆ players; chiavi in sold tutte in players e mai
           nel pool; nessuna vendita duplicata; ogni giocatore in esattamente uno
           dei due; per ogni squadra/ruolo tracciati
           slot_attuali == slot_iniziali - n_vendite_non_ALTRO.
        3. Unsold solo per giocatori ancora nel pool.
        4. Tutti i valori numerici di stato finiti e non negativi (slot >= 0).
        Dopo undo (ripristino snapshot) le invarianti 1-4 devono continuare a
        valere: i test di proprietà lo verificano passo dopo passo.
        """
        problems: list[str] = []
        s = self.state
        cfg = self.cfg

        # struttura minimale: nessuna chiave inattesa, presenza di tutte le attese
        expected_keys = {
            "money_league",
            "money",
            "slots",
            "pool",
            "sold",
            "spent_unknown",
            "unsold",
        }
        if set(s) != expected_keys:
            problems.append(
                f"stato: chiavi inattese (attese {sorted(expected_keys)}, "
                f"trovate {sorted(s)})"
            )
            return problems
        for name, typ in (
            ("money", dict),
            ("slots", dict),
            ("pool", set),
            ("sold", list),
            ("unsold", dict),
        ):
            if not isinstance(s[name], typ):
                problems.append(f"stato: {name!r} non e' un {typ.__name__}")
                return problems

        keys = set(self.players)
        money = s["money"]
        money_league = s["money_league"]
        pool = s["pool"]
        sold = s["sold"]
        slots = s["slots"]
        unsold = s["unsold"]
        spent_unknown = s["spent_unknown"]

        # ---- 1. ledger monetario -------------------------------------------
        sold_sum = 0
        altro_sum = 0
        sold_keys: list[str] = []
        for ent in sold:
            if (
                not isinstance(ent, (tuple, list))
                or len(ent) != 4
                or not isinstance(ent[0], str)
                or type(ent[1]) is not int
                or ent[1] < 1
                or not isinstance(ent[2], str)
                or ent[3] not in ROLE_ORDER
            ):
                problems.append(f"sold: voce malformata {ent!r}")
                continue
            key, price, team, role = ent
            if team == "ALTRO":
                altro_sum += price
            elif team in money:
                pass
            else:
                problems.append(f"sold: squadra {team!r} non tracciata ne' ALTRO")
                continue
            sold_sum += price
            sold_keys.append(key)
            if key not in keys:
                problems.append(f"sold: {key!r} non e' una chiave di players")
            elif key in pool:
                problems.append(f"sold: {key!r} e' ancora nel pool (doppia vendita)")

        bad_money = [
            v
            for v in money.values()
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0)
        ]
        if bad_money:
            problems.append(f"ledger: budget non finiti o negativi: {bad_money!r}")
        money_sum = sum(v for v in money.values() if isinstance(v, (int, float)))
        if money_league != self.total_money0 - sold_sum:
            problems.append(
                f"ledger: money_league != total_money0 - somma prezzi "
                f"({money_league} != {self.total_money0} - {sold_sum})"
            )
        if money_league != money_sum - spent_unknown:
            problems.append(
                f"ledger: money_league != sum(money) - spent_unknown "
                f"({money_league} != {money_sum} - {spent_unknown})"
            )
        if spent_unknown != altro_sum:
            problems.append(
                f"ledger: spent_unknown != somma vendite ALTRO "
                f"({spent_unknown} != {altro_sum})"
            )
        for label, val in (
            ("money_league", money_league),
            ("spent_unknown", spent_unknown),
        ):
            if not (isinstance(val, (int, float)) and math.isfinite(val) and val >= 0):
                problems.append(f"ledger: {label} non finito o negativo: {val!r}")

        # ---- 2. pool / sold / slot -----------------------------------------
        if not pool.issubset(keys):
            problems.append(f"pool: chiavi non in players: {sorted(pool - keys)}")
        if len(sold_keys) != len(set(sold_keys)):
            problems.append("sold: chiavi duplicate (doppia vendita)")
        missing = keys - pool - set(sold_keys)
        if missing:
            problems.append(
                f"pool: giocatori ne' in pool ne' venduti (spariti): {sorted(missing)}"
            )
        if set(slots) != set(money):
            problems.append(
                f"slots: squadre disallineate vs money "
                f"({sorted(slots)} vs {sorted(money)})"
            )
        for team in money:
            ts = slots.get(team)
            if not isinstance(ts, dict):
                problems.append(f"slots: {team!r} non e' un dict")
                continue
            if set(ts) != set(ROLE_ORDER):
                problems.append(f"slots: {team!r} con ruoli inattesi: {sorted(ts)}")
            for role in ROLE_ORDER:
                n = ts.get(role)
                if not (isinstance(n, (int, float)) and math.isfinite(n) and n >= 0):
                    problems.append(
                        f"slots: {team}/{role} non finito o negativo: {n!r}"
                    )
                    continue
                sold_for = sum(
                    1
                    for e in sold
                    if (
                        isinstance(e, (tuple, list))
                        and len(e) == 4
                        and e[2] == team
                        and e[3] == role
                        and type(e[1]) is int
                        and e[1] >= 1
                    )
                )
                expected = (
                    cfg["slots"][role]
                    if cfg.get("game_mode") == "mantra"
                    else cfg["slots"][role] - sold_for
                )
                if n != expected:
                    detail = (
                        "(gli slot P/D/C/A non si consumano in Mantra)"
                        if cfg.get("game_mode") == "mantra"
                        else f"- {sold_for} vendite non-ALTRO"
                    )
                    problems.append(
                        f"slots: {team}/{role} {n} != iniziali "
                        f"{cfg['slots'][role]} {detail}"
                    )
            if cfg.get("game_mode") == "mantra":
                roster = [
                    self.players[e[0]]
                    for e in sold
                    if isinstance(e, (tuple, list))
                    and len(e) == 4
                    and e[2] == team
                    and e[0] in self.players
                ]
                keepers = sum(1 for p in roster if "Por" in player_roles(p))
                if len(roster) > cfg["roster_size"]:
                    problems.append(
                        f"rosa Mantra {team}: {len(roster)} > {cfg['roster_size']} giocatori"
                    )
                if keepers > 15:
                    problems.append(f"rosa Mantra {team}: {keepers} > 15 portieri")
                if len(roster) - keepers > 75:
                    problems.append(
                        f"rosa Mantra {team}: {len(roster) - keepers} > 75 giocatori di movimento"
                    )

        # ---- 3. unsold solo nel pool ---------------------------------------
        for key, n in unsold.items():
            if key not in pool:
                problems.append(f"unsold: {key!r} non e' nel pool")
            if not (isinstance(n, (int, float)) and math.isfinite(n) and n >= 0):
                problems.append(
                    f"unsold: contatore di {key!r} non finito o negativo: {n!r}"
                )

        return problems

    # ----------------------------------------------------------------- report
    def status(self):
        cfg = self.cfg
        lines = []
        infl = self.inflation()
        money_rem = self.state["money_league"]
        value_rem = self._value_rem()
        infl_pma = self.inflation_pma()
        value_rem_pma = self._value_rem_pma()
        lines.append(
            f"Inflazione vs PFC: x{infl:.2f}   vs PMA: x{infl_pma:.2f}   "
            f"(crediti residui {money_rem} / valore residuo a PFC {value_rem} "
            f"e a PMA {value_rem_pma:.0f}, norm. all'inizio = x1.00)"
        )
        lines.append(
            f"Somma PFC del listone {self.total_value0:.0f} cr vs budget lega "
            f"{self.total_money0} cr: il mercato si puo' permettere solo una parte dei PFC."
        )
        spent_by_role = defaultdict(int)
        for _, price, _, role in self.state["sold"]:
            spent_by_role[role] += price
        lines.append(
            f"{'Ruolo':<15}{'Slot aperti':>12}{'Utilizz.':>9}{'Scarsità':>9}"
            f"{'Qualità':>9}{'Top-20':>8}{'FIX med':>9}{'Speso':>8}{'% pool':>8}"
        )
        for role in ROLE_ORDER:
            group = [
                self.players[k]
                for k in self.state["pool"]
                if self.players[k]["ruolo"] == role
            ]
            usable = [p for p in group if p["base"] >= cfg["quality_floor"]]
            scarcities = sorted(self.scarcity(p) for p in usable)
            med = scarcities[len(scarcities) // 2] if scarcities else 1.0
            spent_pct = 100 * spent_by_role[role] / max(self.role_pool0[role], 1)
            lines.append(
                f"{ROLE_LABEL[role]:<15}{self._demand(role):>12}{len(usable):>9}"
                f"{f'x{med:.2f}':>9}{self.quality_left(role):>8}%"
                f"{self.starters_left(role):>8}{f'x{self.fix_mean(role):.2f}':>9}"
                f"{spent_by_role[role]:>8}{spent_pct:>7.0f}%"
            )
        lines.append(
            "Slot titolari per squadra: "
            + "/".join(str(cfg["formation"][r]) for r in ROLE_ORDER)
            + f" (P/D/C/A) — Top-{20 if cfg['starter_slot_max'] == 2 else cfg['starter_slot_max'] * 10}"
            f" = giocatori rimasti di qualita' titolare"
        )
        rows = sorted(self.state["money"].items(), key=lambda kv: -kv[1])
        budget_line = "Budget: " + "  ".join(f"{t} {m}" for t, m in rows)
        if self.state["spent_unknown"]:
            budget_line += f"  |  ALTRO speso {self.state['spent_unknown']} cr"
        lines.append(budget_line)
        return "\n".join(lines)

    def format_eval(self, p, team=None):
        e = self.evaluate(p, team)
        if "error" in e:
            return e["error"]
        cfg = self.cfg
        # stesso sconto calcolato in evaluate(): numero certo, senza accessi su dict
        disc = cfg["unsold_discount"] ** self.state["unsold"].get(p["pid"], 0)
        disc_note = f"  (sconto invenduto x{disc:.2f})" if disc < 1 else ""
        slot_lbl = SLOT_LABEL.get(p["slot"], "fuori lista (oltre il quarto slot)")
        status_lbl = STATUS_LABEL.get(p["status"].upper(), "") or p["status"]
        pma_note = (
            (
                "sottoprezzato vs mercato"
                if p["dpfcpma"] < 0
                else "sopravvalutato vs mercato"
            )
            if p["dpfcpma"]
            else "in linea col mercato"
        )
        mkt = e["market"]
        fan = e["fantasy"]
        mt = e["my_team"]
        rng = mkt["range"]
        range_src = {
            "scaled_pfc_range": "range PFC scalato",
            "unc_pfc": "incertezza unc_pfc",
            "conservative_fallback": "fallback conservativo (±20%)",
        }.get(mkt["range_source"], mkt["range_source"])
        lines = [
            (
                f"{p['nome']} ({ROLE_SING[p['ruolo']]}, {p['squadra'] or '—'})  "
                f"{p['fascia'] or '—'}{(' · ' + status_lbl) if status_lbl else ''}"
            ),
            "── Mercato previsto ──",
            (
                f"  Prezzo di mercato atteso: {mkt['expected']} cr  "
                f"(range {rng['lo']}–{rng['hi']} cr, fonte: {range_src})"
            ),
            (
                f"  Valore base (PFC): {e['base']} cr  range PFC {p['pfc_range'] or '—'}"
                f"   |  PMA {p['pma']:.0f} cr range {p['pma_range'] or '—'}  "
                f"({pma_note}, delta {p['dpfcpma']:+.0f})"
            ),
            f"  Inflazione:       x{e['infl']:.2f}",
            (
                f"  Scarsità:       x{e['scarc']:.2f}  (slot {ROLE_SING[p['ruolo']].lower()} aperti: "
                f"{e['demand']}, alternative slot<={p['slot']}: {e['alt']} per {e['alt_value']} cr){disc_note}"
            ),
            (
                f"  Fonte incertezza: unc_pfc {mkt['unc_pfc']} cr  "
                f"(range numerico PFC {mkt['pfc_lo']}–{mkt['pfc_hi']})"
            ),
            "── Valore fantacalcistico ──",
            f"  Utility: {fan['utility']:.2f}/1 (score {fan['score']}/100)",
            (
                f"  Slot consigliato: {p['slot']} ({slot_lbl})   Titolarita' attesa: {p['tit']:.0f}% "
                f"(TIX {p['tix']:.0f})   FM attesa: {p['expfm']:.2f} (FIX {p['fix']:.0f}, "
                f"contributo {p['fix_contrib']:+.2f}/gior.)"
            ),
            f"  Rigori: {p['pen_prob']:.0f}%   Calci da fermo: {p['fk_prob']:.0f}%",
            f"  Status: {status_lbl or '—'} (rischio {fan['risk']:.2f})   Fascia: {fan['fascia'] or '—'}",
            "── Valore per la mia rosa ──",
        ]
        if e["tracked"]:
            bd = e["scarcity_breakdown"]
            lines.append(
                f"  Scarsità per la mia rosa: x{e['scarcity_team']:.2f}  "
                f"(lega x{e['scarc']:.2f} × team x{bd['team_component']:.2f}; "
                f"concorrenti {bd['competition']['competitors']} su {bd['competition']['total']})"
            )
        if mt["team_value"] is None:
            lines.append(
                "  Valore per la rosa: non calcolabile (squadra non tracciata: "
                "nessuna rosa da simulare)."
            )
        elif e["covered"]:
            lines.append(
                f"  Valore per la rosa: 0 cr (ruolo {ROLE_SING[p['ruolo']].lower()} coperto: "
                f"slot {cfg['slots'][p['ruolo']]}/{cfg['slots'][p['ruolo']]} esauriti)."
            )
        else:
            alt = mt["alternative"]
            alt_txt = (
                f"migliore alternativa {alt['nome']} (utility {alt['utility']:.2f})"
                if alt
                else "nessuna alternativa residua"
            )
            lines.append(
                f"  Valore per la rosa: {mt['team_value']} cr  "
                f"(mercato atteso {mt['expected']} × ratio {mt['value_ratio']:.2f} × "
                f"gain {mt['marginal_gain']:.2f} × bisogno {mt['need']:.2f})"
            )
            lines.append(
                f"    {alt_txt}  ·  buchi titolari {mt['holes_ratio']:.0%}  ·  "
                f"alternative {mt['n_alt']} nel ruolo"
            )
        if e["covered"]:
            lines.append(
                f"  {cfg['io']}: slot {ROLE_SING[p['ruolo']].lower()} esauriti "
                f"(coperti {cfg['slots'][p['ruolo']]}/{cfg['slots'][p['ruolo']]}) — solo a scopo informativo."
            )
        elif not e["tracked"]:
            lines.append(
                "  Squadra non tracciata: nessun tetto calcolabile "
                f"(usa 'venduto <nome> <prezzo> {cfg['io']}' per registrare a te)."
            )
        else:
            note = (
                "  (budget ridotto: sotto il prezzo suggerito)"
                if e["maxbid"] < e["suggested"]
                else ""
            )
            lines.append(
                f"  Max sostenibile — Max offerta {cfg['io']}:  {e['maxbid']} cr{note}  "
                f"(crediti {e['money']}, slot da coprire {e['slots_left']})"
            )
            # WP6: breakdown dei 4 cap trasparenti (una riga per cap)
            mb = e.get("maxbid_breakdown") or {}
            if mb.get("final") is not None:
                lines.append(
                    f"    cap mercato (x{cfg['aggression']:.2f}): {mb['market_cap']} cr"
                )
                lines.append(
                    f"    cap riserva (completamento rosa): {mb['reserve_cap']} cr"
                )
                lines.append(
                    f"    cap ruolo ({mb['role']['role']}): {mb['role_cap']} cr "
                    f"(target {mb['role']['target']}, speso {mb['role']['spent']}, "
                    f"rilasciato {mb['role']['released']})"
                )
                alt = mb.get("alternative") or {}
                if mb["opportunity_cap"] is None:
                    lines.append("    cap opportunità: nessuna alternativa residua")
                else:
                    lines.append(
                        f"    cap opportunità: {mb['opportunity_cap']} cr "
                        f"(alternativa {alt.get('nome') or '—'} a "
                        f"{alt.get('prezzo') or '—'} cr, utility {alt.get('utility') or '—'})"
                    )
                lines.append(
                    f"    max sostenibile finale: {e['maxbid']} cr [vincola: {mb['binding_cap']}]"
                )
        return "\n".join(lines)


# ----------------------------------------------------------------------- CLI
HELP = """Comandi:
  prezzo <nome>                        valutazione live del giocatore
  venduto <nome> <prezzo> [squadra]    registra l'acquisto (alias: sold)
  invenduto <nome>                     registra il rilancio mancato (alias: unsold)
  stato                                inflazione, scarsità/qualità residua per ruolo, budget
  config sistema=mantra rosa=28 modulo=4-3-3 squadre=8 budget=500 io=IO
                                       riconfigura e azzera l'asta
  undo                                 annulla l'ultima operazione
  aiuto                                questo help      esci  per uscire"""

CONFIG_KEYS = {
    # chiavi di destinazione = chiavi normalizzate dello schema league_config
    # (teams, budget, slots, formation, tit_cov_threshold, io)
    "squadre": "teams",
    "budget": "budget",
    "sistema": "game_mode",
    "rosa": "roster_size",
    "modulo": "mantra_formation",
    "slotp": ("slots", "P"),
    "slotd": ("slots", "D"),
    "slotc": ("slots", "C"),
    "slota": ("slots", "A"),
    "formazp": ("formation", "P"),
    "formazd": ("formation", "D"),
    "formazc": ("formation", "C"),
    "formaza": ("formation", "A"),
    "soglia-tit": "tit_cov_threshold",
    "io": "io",
}


def config_overrides(auction, toks):
    """Parsa i token 'chiave=valore' del comando config in overrides sulle chiavi
    normalizzate di league_config. Ritorna None se un token e' invalido
    (l'errore e' gia' stato stampato)."""
    overrides = {}
    for tok in toks:
        if "=" not in tok:
            print(f"Chiave non valida: {tok} (formato chiave=valore)")
            return None
        k, v = tok.split("=", 1)
        if k not in CONFIG_KEYS:
            print(f"Chiave sconosciuta: {k}")
            return None
        target = CONFIG_KEYS[k]
        if target in ("io", "game_mode", "mantra_formation"):
            val = v.strip()
        else:
            try:
                val = int(v)
            except ValueError:
                print(f"Valore non intero: {k}={v}")
                return None
        if isinstance(target, tuple):
            overrides.setdefault(target[0], dict(auction.cfg[target[0]]))[target[1]] = (
                val
            )
        else:
            overrides[target] = val
    return overrides


def proposed_cfg(auction, overrides):
    """Config completa proposta dal comando config: la config corrente del motore
    + i soli override del REPL, normalizzati (uno schema unico, league_config)."""
    return normalize({**auction.cfg, **overrides})


def config_errors(auction, overrides):
    """Errori strutturali + di fattibilita' della config proposta (profilo pubblico,
    come API e validate_config, sulla config corrente del REPL). NON muta l'asta:
    una config rifiutata non azzera."""
    cfg = proposed_cfg(auction, overrides)
    errors = list(validate(cfg))
    errors += feasibility(cfg, list(auction.players.values())).errors
    return errors


def run_repl(auction):
    print(HELP)
    print("\n" + auction.status() + "\n")
    while True:
        try:
            line = input("asta> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        cmd, *args = shlex.split(line)
        low = cmd.lower()
        try:
            if low in ("esci", "exit", "quit", "q"):
                return
            elif low in ("aiuto", "help", "h"):
                print(HELP)
            elif low in ("prezzo", "price", "p"):
                if not args:
                    print("Uso: prezzo <nome>")
                    continue
                p = auction.find(" ".join(args))
                print("Giocatore non trovato." if p is None else auction.format_eval(p))
            elif low in ("venduto", "sold"):
                toks = args
                if len(toks) < 2 or not toks[-1].isdigit() and not toks[-2].isdigit():
                    print("Uso: venduto <nome> <prezzo> [squadra]")
                    continue
                if toks[-1].isdigit() and not toks[-2].isdigit():
                    name, price, team = " ".join(toks[:-1]), int(toks[-1]), None
                else:
                    name, price, team = " ".join(toks[:-2]), int(toks[-2]), toks[-1]
                p = auction.find(name)
                if p is None:
                    print("Giocatore non trovato.")
                    continue
                team = auction.mark_sold(p, price, team)
                print(f"OK: {p['nome']} -> {team} per {price} cr")
            elif low in ("invenduto", "unsold"):
                if not args:
                    print("Uso: invenduto <nome>")
                    continue
                p = auction.find(" ".join(args))
                if p is None:
                    print("Giocatore non trovato.")
                    continue
                auction.mark_unsold(p)
                print(
                    f"OK: {p['nome']} resta invenduto (prossima valutazione scontata)."
                )
            elif low in ("stato", "status"):
                print(auction.status())
            elif low == "config":
                overrides = config_overrides(auction, args)
                if overrides is None:
                    continue
                # stessa validazione del ciclo di vita CLI/web (profilo pubblico,
                # sulla config corrente + override): su errore si stampa la lista
                # errori e l'asta NON viene azzerata
                errors = config_errors(auction, overrides)
                if errors:
                    for e in errors:
                        print(f"Errore: {e}")
                    continue
                auction = Auction(
                    list(auction.players.values()), **proposed_cfg(auction, overrides)
                )
                print("Configurazione aggiornata, asta azzerata.\n")
                print(auction.status())
            elif low == "undo":
                print(
                    "Operazione annullata."
                    if auction.undo()
                    else "Niente da annullare."
                )
            else:
                print(f"Comando sconosciuto: {cmd}. 'aiuto' per l'elenco.")
        except AmbiguousName as e:
            print(f"Nome ambiguo, specifica meglio. Opzioni: {e}")
        except AuctionError as e:
            print(f"Errore: {e}")
        except ConfigError as e:
            print(f"Errore: {e}")


def _demo_buyer(auction, role):
    """Squadra piu' ricca che puo' ancora comprare (crediti >= 1 e uno slot libero)."""
    money = auction.state["money"]
    candidates = [
        t for t in money if money[t] >= 1 and auction.state["slots"][t][role] > 0
    ]
    return max(candidates, key=lambda t: money[t]) if candidates else None


def run_demo(auction):
    print("=== SIMULAZIONE ASTA (demo) ===\n")
    print("[1] Situazione iniziale — inflazione x1.00, scarsità x1.00")
    stars = [
        "DIMARCO",
        "PAZ N.",
        "MCTOMINAY",
        "MALEN",
        "DYBALA",
        "SVILAR",
        "BASTONI",
        "CALHANOGLU",
        "VICARIO",
        "MARTINEZ L.",
        "PULISIC",
        "BARELLA",
        "ORSOLINI",
        "THURAM",
    ]
    print(
        "    PFC delle stelle: "
        + ", ".join(
            f"{n} {auction.find(n)['base']}cr" for n in stars if auction.find(n)
        )
    )
    probe_c = auction.find("BARELLA")
    print("\n    Esempio prima delle vendite:")
    print("    " + auction.format_eval(probe_c).replace("\n", "\n    "))

    print("\n[2] Fase 1 — le stelle vengono battute al PFC (o poco sopra)")
    for n in stars:
        p = auction.find(n)
        if p:
            buyer = _demo_buyer(auction, p["ruolo"])
            if buyer is None:
                break
            price = min(round(p["base"] * 1.1), auction.state["money"][buyer])
            auction.mark_sold(p, price, buyer)
    print("    " + auction.status().replace("\n", "\n    "))
    probe = auction.find("ZIELINSKI") or auction.find("CALHANOGLU")
    print(
        f"\n    Stesso tipo di giocatore dopo le vendite ({probe['nome']}, PFC {probe['base']}):"
    )
    print("    " + auction.format_eval(probe).replace("\n", "\n    "))

    print(
        "\n[3] Fase 2 — si consuma la fascia titolare dei difensori (slot <= 2, a +15%)"
    )
    rich_d = sorted(
        (
            p
            for k, p in auction.players.items()
            if k in auction.state["pool"] and p["ruolo"] == "D" and p["slot"] <= 2
        ),
        key=lambda p: -p["base"],
    )
    for p in rich_d:
        buyer = _demo_buyer(auction, p["ruolo"])
        if buyer is None:
            break
        auction.mark_sold(
            p, min(round(p["base"] * 1.15), auction.state["money"][buyer]), buyer
        )
    print("    " + auction.status().replace("\n", "\n    "))
    best_d = sorted(
        (
            auction.players[k]
            for k in auction.state["pool"]
            if auction.players[k]["ruolo"] == "D"
        ),
        key=lambda p: -p["base"],
    )
    if best_d:
        print(
            f"\n    Miglior difensore rimasto ({best_d[0]['nome']}, PFC {best_d[0]['base']} cr):"
        )
        print("    " + auction.format_eval(best_d[0]).replace("\n", "\n    "))

    print(
        "\n[4] Fase 3 — un giocatore resta invenduto due volte: lo strumento lo sconta"
    )
    target = sorted(
        (
            auction.players[k]
            for k in auction.state["pool"]
            if auction.players[k]["ruolo"] == "C" and auction.players[k]["base"] >= 15
        ),
        key=lambda p: -p["base"],
    )
    if target:
        p = target[0]
        print(f"    {p['nome']} (PFC {p['base']} cr):")
        auction.mark_unsold(p)
        auction.mark_unsold(p)
        print("    " + auction.format_eval(p).replace("\n", "\n    "))

    print("\nDemo conclusa: stesso PFC, prezzo live diverso per inflazione e scarsità.")
    print("Avvia la sessione reale con: uv run scripts/live_auction.py")


def main():
    ap = argparse.ArgumentParser(
        description="Asta live Fantacalcio 2026/27 (Listone Fantaculo)"
    )
    ap.add_argument(
        "--csv",
        default=DEFAULT_CSV,
        help="CSV canonico del listone (import_listone.py)",
    )
    ap.add_argument("--prezzo", metavar="NOME", help="valutazione singola e poi esce")
    ap.add_argument("--demo", action="store_true", help="simulazione dimostrativa")
    ap.add_argument("--squadre", type=int, default=DEFAULTS["teams"])
    ap.add_argument("--budget", type=int, default=DEFAULTS["budget"])
    ap.add_argument("--io", default=DEFAULTS["io"])
    args = ap.parse_args()

    players = load_players(args.csv)
    cfg = dict(DEFAULTS)
    cfg.update(teams=args.squadre, budget=args.budget, io=args.io)
    try:
        Auction.validate_config(players, cfg)
    except ConfigError as e:
        print(f"Configurazione non valida: {e}", file=sys.stderr)
        sys.exit(1)
    auction = Auction(players, teams=args.squadre, budget=args.budget, io=args.io)
    if args.demo:
        run_demo(auction)
    elif args.prezzo:
        try:
            p = auction.find(args.prezzo)
            print("Giocatore non trovato." if p is None else auction.format_eval(p))
        except AmbiguousName as e:
            print(f"Nome ambiguo. Opzioni: {e}")
    else:
        run_repl(auction)


if __name__ == "__main__":
    main()
