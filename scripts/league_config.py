"""Schema di configurazione della lega — unico validatore per dominio, CLI e web (WP2).

Questo modulo e' l'unica fonte di verita' per la configurazione della lega:

- ``DEFAULTS``, ``ROLE_ORDER``, ``ROLE_LABEL``, ``norm`` (ri-esportati da
  ``live_auction`` e ``web_auction`` per compatibilita').
- ``normalize(cfg)`` — rende canonica una configurazione parziale: completa con
  i default, allinea i nomi squadra (strip, squadra ``io`` presente), riempie
  slot/formation per ruolo. Non valida: tipi e limiti restano a ``validate()``.
- ``validate(cfg, profile=...)`` — errori STRUTTURALI della configurazione.
- ``feasibility(cfg, players, profile=...)`` — errori+warning di FATTIBILITA'
  rispetto al listone (copertura per ruolo del pool, budget rispetto al costo
  minimo della rosa).
- ``minimum_roster_cost(cfg, players)`` — costo minimo (a squadra) per
  completare la rosa, condiviso con WP6 (floor per ruolo via
  ``cfg["role_floor_price"]``; WP2 usa il floor piatto ``min_slot_price=2``).

Profili (regola trasversale, niente logica duplicata):
- ``profile="public"`` (default) — API pubblica CLI/web/validate_config:
  teams 2-20, budget 10-10000, pool sufficiente per ruolo, budget >= costo
  minimo rosa.
- ``profile="engine"`` — profilo del costruttore interno dell'``Auction``:
  ammette simulazioni (teams >= 1, budget >= 1, pool parziale: nessuna
  fattibilita' di pool/budget). Le regole STRUTTURALI valgono identiche a
  ``public``: in particolare ``formation <= slots`` e' un'invariante e non
  viene mai disabilitata. Le entry point (CLI, web, ``validate_config``)
  applicano sempre il profilo pubblico PRIMA di costruire il motore: una
  configurazione invalida non puo' arrivare a un motore dal percorso reale.

Nessuna configurazione invalida puo' azzerare l'asta: chi applica una config
(CLI ``config``, ``POST /api/config``, ``validate_config``) valida prima e, su
errore, lascia motore e stato intatti.
"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Any
from unicodedata import normalize as _unicode_normalize

# WP8: validazione del blocco calibrazione delegata a ``calibration`` (unica
# fonte di verita' dei parametri calibration_*). ``calibration`` e' puro
# stdlib e non importa league_config: nessun ciclo.
import calibration

# ------------------------------------------------------------- schema della lega
ROLE_ORDER = ["P", "D", "C", "A"]

ROLE_LABEL = {
    "P": "Portieri",
    "D": "Difensori",
    "C": "Centrocampisti",
    "A": "Attaccanti",
}

# Range dell'API pubblica (da NON indebolire): il profilo "engine" rilassa
# solo l'estremo inferiore di teams/budget (simulazioni); mai le invarianti
# strutturali come formation <= slots.
MIN_TEAMS = 2
MAX_TEAMS = 20
MIN_BUDGET = 10
MAX_BUDGET = 10000

DEFAULTS = {
    "teams": 8,
    "budget": 500,
    "slots": {"P": 3, "D": 8, "C": 8, "A": 6},  # rosa classica da 25
    "formation": {"P": 1, "D": 4, "C": 4, "A": 2},  # titolari per ruolo (352)
    "tit_cov_threshold": 70,  # titolarita' minima per "coprire" uno slot
    "starter_slot_max": 2,  # slot <= 2 = qualita' titolare (top-20 del ruolo)
    "value_floor": 5,  # PFC minimo per entrare nel "valore in carta"
    "quality_floor": 8,  # PFC minimo per essere "utilizzabile" nello stato
    "scarcity_beta": 0.6,  # smorza il moltiplicatore di scarsità (componente slot)
    "scarcity_min": 0.6,  # sconto massimo per ruolo sovrabbondante
    "scarcity_max": 3.0,  # premio massimo per scarsità
    "scarcity_value_beta": 0.5,  # smorza la componente valore (PFC delle alternative)
    "scarcity_value_min": 0.8,  # pavimento componente valore
    "scarcity_value_max": 2.0,  # tetto componente valore
    "aggression": 1.25,  # max offerta = prezzo suggerito x aggression
    "min_slot_price": 2,  # riserva per ogni slot ancora da coprire (floor WP2)
    "unsold_discount": 0.85,  # sconto per ogni volta che il giocatore resta invenduto
    "io": "IO",  # nome della propria squadra
    "team_names": None,  # nomi personalizzati delle squadre (lista ordinata); None = IO, T1, T2...
}

# Chiavi dello schema config: tutto il resto passa through (tuning del motore).
_SCHEMA_KEYS = {
    "teams",
    "budget",
    "slots",
    "formation",
    "tit_cov_threshold",
    "io",
    "team_names",
}


def norm(text):
    """Normalizzazione identita' (nomi giocatori e squadre): ASCII lowercase."""
    text = _unicode_normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def _count_roles(players):
    """Conteggio per ruolo del libro giocatori (ruoli non P/D/C/A ignorati)."""
    counts = dict.fromkeys(ROLE_ORDER, 0)
    for p in players:
        role = str(p.get("ruolo") or "").strip().upper()
        if role in counts:
            counts[role] += 1
    return counts


# ------------------------------------------------------------------ normalize
def normalize(cfg: dict[str, Any]) -> dict[str, Any]:
    """Configurazione canonica di una configurazione parziale.

    - Completa con i DEFAULTS (chiavi mancanti, slot/formation per ruolo).
    - Allinea ``io`` (strip) e ``team_names`` (strip; derivati ``[io, T1, ...]``
      quando non forniti, come nel ciclo di vita attuale del motore).
    - Preserva le chiavi extra (tuning: aggression, scarcity_*, ...).
    Idempotente. NON valida: i tipi/limiti restano a ``validate()``.
    """
    out = dict(DEFAULTS)
    for key in ("teams", "budget", "tit_cov_threshold"):
        if key in cfg:
            out[key] = cfg[key]
    # slots/formation parziali: i ruoli mancanti prendono il default; se il
    # valore fornito non e' un dict lo si preserva perche' validate() lo segnali.
    if "slots" in cfg:
        slots = cfg["slots"]
        out["slots"] = (
            {**DEFAULTS["slots"], **slots} if isinstance(slots, dict) else slots
        )
    if "formation" in cfg:
        formation = cfg["formation"]
        out["formation"] = (
            {**DEFAULTS["formation"], **formation}
            if isinstance(formation, dict)
            else formation
        )
    io = cfg.get("io")
    if io is not None:
        out["io"] = str(io).strip() or DEFAULTS["io"]
    if "team_names" in cfg:
        names = cfg["team_names"]
        out["team_names"] = (
            [str(n).strip() for n in names] if isinstance(names, list) else names
        )
    else:
        teams = out["teams"]
        if type(teams) is int:
            out["team_names"] = [out["io"]] + [f"T{i}" for i in range(1, teams)]
        else:
            # teams non valido: validate() lo segnala; nomi minimi per non crashare
            out["team_names"] = [out["io"]]
    for k, v in cfg.items():
        if k not in _SCHEMA_KEYS:
            out[k] = v
    return out


# ------------------------------------------------------------------- validate
def validate(cfg: dict[str, Any], *, profile: str = "public") -> list[str]:
    """Errori STRUTTURALI della configurazione (lista vuota = ok).

    ``profile="public"`` (default): range dell'API pubblica (2-20 squadre,
    10-10000 crediti). ``profile="engine"``: profilo del costruttore interno,
    teams >= 1 e budget >= 1 (simulazioni). In entrambi valgono le stesse
    invarianti strutturali: slot/formation per ruolo interi >= 0,
    formation <= slots, nomi squadra unici con ``io`` presente.
    Non muta nulla.
    """
    errors: list[str] = []
    teams = cfg.get("teams")
    if type(teams) is not int or teams < 1 or teams > MAX_TEAMS:
        errors.append(
            f"il numero di squadre deve essere un intero tra 1 e {MAX_TEAMS}"
            + (
                ""
                if profile != "public"
                else f" ({MIN_TEAMS}-{MAX_TEAMS} per l'API pubblica)"
            )
        )
    elif profile == "public" and teams < MIN_TEAMS:
        errors.append(
            f"il numero di squadre deve essere un intero tra {MIN_TEAMS} e {MAX_TEAMS}"
        )
    budget = cfg.get("budget")
    if type(budget) is not int or budget < 1 or budget > MAX_BUDGET:
        errors.append(
            f"il budget deve essere un intero tra 1 e {MAX_BUDGET} crediti"
            + (
                ""
                if profile != "public"
                else f" ({MIN_BUDGET}-{MAX_BUDGET} per l'API pubblica)"
            )
        )
    elif profile == "public" and budget < MIN_BUDGET:
        errors.append(
            f"il budget deve essere un intero tra {MIN_BUDGET} e {MAX_BUDGET} crediti"
        )

    slots = cfg.get("slots")
    formation = cfg.get("formation")
    if not isinstance(slots, dict):
        errors.append("slots non valido: serve un dict con i ruoli P/D/C/A")
    if not isinstance(formation, dict):
        errors.append("formation non valida: serve un dict con i ruoli P/D/C/A")

    if isinstance(slots, dict):
        for role in ROLE_ORDER:
            n = slots.get(role)
            if type(n) is not int or n < 0:
                errors.append(
                    f"slot del ruolo {role} non valido: serve un intero >= 0 (trovato {n!r})"
                )
    if isinstance(formation, dict):
        for role in ROLE_ORDER:
            n = formation.get(role)
            if type(n) is not int or n < 0:
                errors.append(
                    f"formazione del ruolo {role} non valida: serve un intero >= 0 (trovato {n!r})"
                )
            elif (
                isinstance(slots, dict)
                and type(slots.get(role)) is int
                and n > slots[role]
            ):
                # invariante strutturale in OGNI profilo: mai piu' titolari che slot
                errors.append(
                    f"formazione {ROLE_LABEL[role]} ({n} titolari) supera gli slot "
                    f"della rosa ({slots[role]}): squadra impossibile."
                )

    tit = cfg.get("tit_cov_threshold")
    if tit is not None and (type(tit) is not int or tit < 0 or tit > 100):
        errors.append(
            f"tit_cov_threshold deve essere un intero tra 0 e 100 (trovato {tit!r})"
        )

    # ---- WP6: floor per ruolo e pesi di allocazione (opzionali, pass-through) ----
    # Backward-compat: l'assenza non cambia nulla; la presenza impone i vincoli
    # documentati (floor >= 1 per ruolo noto; pesi >= 0 per ruolo noto, somma > 0).
    role_floors = cfg.get("role_floor_price")
    if role_floors is not None:
        if not isinstance(role_floors, dict):
            errors.append("role_floor_price non valido: serve un dict {ruolo: floor}")
        else:
            for role, v in role_floors.items():
                if role not in ROLE_ORDER:
                    errors.append(
                        f"role_floor_price: ruolo sconosciuto {role!r} (attesi P/D/C/A)"
                    )
                elif not (isinstance(v, (int, float)) and isfinite(v) and v >= 1):
                    errors.append(
                        f"role_floor_price: floor del ruolo {role} non valido: "
                        f"serve un numero >= 1 (trovato {v!r})"
                    )
    role_weights = cfg.get("role_budget_weights")
    if role_weights is not None:
        if not isinstance(role_weights, dict):
            errors.append("role_budget_weights non valido: serve un dict {ruolo: peso}")
        else:
            total = 0.0
            for role, v in role_weights.items():
                if role not in ROLE_ORDER:
                    errors.append(
                        f"role_budget_weights: ruolo sconosciuto {role!r} "
                        f"(attesi P/D/C/A)"
                    )
                elif not (isinstance(v, (int, float)) and isfinite(v) and v >= 0):
                    errors.append(
                        f"role_budget_weights: peso del ruolo {role} non valido: "
                        f"serve un numero >= 0 (trovato {v!r})"
                    )
                elif v > 0:
                    total += v
            # i ruoli mancanti valgono 0; serve almeno un peso positivo (somma > 0)
            if not (isfinite(total) and total > 0):
                errors.append("role_budget_weights: la somma dei pesi deve essere > 0")

    names = cfg.get("team_names")
    if names is not None and not isinstance(names, list):
        errors.append("team_names non valido: serve una lista di nomi")
    elif isinstance(names, list) and type(teams) is int:
        if len(names) != teams:
            errors.append(f"servono {teams} nomi squadra, ricevuti {len(names)}")
        elif len({norm(n) for n in names}) != len(names):
            errors.append("nomi squadra duplicati")
        elif cfg.get("io") is not None and cfg["io"] not in names:
            errors.append(f"la squadra '{cfg['io']}' non e' tra i nomi della lega")

    # ---- WP8: blocco advisory calibrazione (opzionale, pass-through) -------
    # ``use_calibration_in_price`` e' un flag BOOLEANO esposto ma NON applicato
    # in questo WP (advisory_gate_off); i parametri ``calibration_*`` sono
    # validati da ``calibration.parse_calibration_params`` (default documentati:
    # bande, k, half_life, bounds, ci, phase_mode) quando presenti.
    use_cal = cfg.get("use_calibration_in_price")
    if use_cal is not None and type(use_cal) is not bool:
        errors.append(
            "use_calibration_in_price deve essere un booleano "
            f"(trovato {use_cal!r}) — il flag e' advisory-only in questo WP"
        )
    _cal_cfg, cal_errors = calibration.parse_calibration_params(cfg)
    errors += cal_errors
    return errors


# ------------------------------------------------------------------- costi
def minimum_roster_cost(cfg: dict[str, Any], players: list[Any] | None = None) -> int:
    """Costo minimo (a squadra) per completare la rosa: per ogni ruolo
    ``min(slots[r], disponibili_r)`` giocatori al prezzo minimo di ruolo.
    Default WP2: floor piatto ``min_slot_price`` (=2). WP6 potra' passare
    floor per ruolo via ``cfg["role_floor_price"]`` ({ruolo: floor}).

    ``players=None`` assume il pool completo (upper-bound ottimistico della
    rosa); con i player usa la disponibilita' reale per ruolo.
    """
    counts = _count_roles(players) if players else None
    role_floors = cfg.get("role_floor_price") or {}
    floor_default = cfg.get("min_slot_price", 2)
    slots = cfg.get("slots")
    total = 0
    if isinstance(slots, dict):
        for role in ROLE_ORDER:
            s = slots.get(role)
            if type(s) is not int or s < 0:
                continue  # validate() lo segnala; qui calcolo difensivo
            n = s if counts is None else min(s, counts[role])
            total += n * role_floors.get(role, floor_default)
    return total


# ---------------------------------------------------------------- feasibility
@dataclass(frozen=True)
class Feasibility:
    """Esito della fattibilita' di una configurazione rispetto al listone.
    ``errors`` bloccano (config rifiutata), ``warnings`` sono esposti ma non
    bloccanti (es. budget rigido ma raggiungibile, margine zero su un ruolo)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def all(self) -> list[str]:
        return list(self.errors) + list(self.warnings)

    def as_dict(self) -> dict[str, list[str]]:
        return {"errors": list(self.errors), "warnings": list(self.warnings)}


def feasibility(
    cfg: dict[str, Any], players: list[Any], *, profile: str = "public"
) -> Feasibility:
    """Errori+warning di fattibilita' della config rispetto al pool di giocatori.

    Bloccanti (errors):
    - per ogni ruolo: disponibili nel listone >= teams x slots[ruolo];
    - budget >= costo minimo completamento rosa (floor corrente min_slot_price).
    Warning: margine zero per ruolo (disponibili == servono) e budget rigido
    (costo minimo > 80% del budget). Con ``profile="engine"`` non controlla
    nulla (il costruttore del motore ammette pool parziali per simulazioni;
    la fattibilita' e' applicata solo dalle entry point pubbliche).
    """
    if profile != "public":
        return Feasibility()

    errors: list[str] = []
    warnings: list[str] = []
    teams = cfg.get("teams")
    budget = cfg.get("budget")
    slots = cfg.get("slots")
    counts = _count_roles(players)

    if type(teams) is int and isinstance(slots, dict):
        for role in ROLE_ORDER:
            s = slots.get(role)
            if type(s) is not int:
                continue  # validate() lo segnala come errore strutturale
            need = teams * s
            avail = counts[role]
            if avail < need:
                errors.append(
                    f"listone insufficiente per {ROLE_LABEL[role]}: {teams} squadre x "
                    f"{s} slot = {need} giocatori necessari, "
                    f"disponibili {avail} nel listone."
                )
            elif need > 0 and avail == need:
                warnings.append(
                    f"margine zero per {ROLE_LABEL[role]}: disponibili esattamente "
                    f"{avail} giocatori, quanti ne servono (nessuna alternativa residua)."
                )

    if type(teams) is int and type(budget) is int and isinstance(slots, dict):
        min_cost = minimum_roster_cost(cfg, players)
        if min_cost > budget:
            errors.append(
                f"budget insufficiente per completare la rosa: servono almeno "
                f"{min_cost} cr a squadra (costo minimo), budget {budget} cr."
            )
        elif min_cost > 0 and min_cost / budget > 0.8:
            warnings.append(
                f"budget rigido: il costo minimo della rosa ({min_cost} cr) assorbe "
                f"il {round(100 * min_cost / budget)}% del budget ({budget} cr)."
            )
    return Feasibility(errors=errors, warnings=warnings)
