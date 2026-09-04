"""Calibrazione adattiva del mercato dalle vendite reali (WP8) — advisory-only.

Impara dalle vendite reali un **fattore di mercato** per cella
``(ruolo x fascia prezzo x fase asta)``, robusto agli outlier e con
confidenza esplicita. In questo WP il fattore e' SOLO advisory: non entra
mai nel prezzo (``applied=False``, ``reason="advisory_gate_off"``); il flag
``use_calibration_in_price`` (default False) e' esposto ma NON applicato
finche' il gate del WP9 non lo consente.

Modulo puro stdlib: niente numpy/ML e nessun import da live_auction /
valuation / web_auction. I dati entrano come dict semplici (eventi di
vendita e config di lega), quindi non esistono cicli di import e il modulo
e' testabile isolato.

Formula (documentata e deterministica)
--------------------------------------
Per ogni vendita ATTIVA (``premium_pfc`` = prezzo / base):

1. **Segmentazione** — la cella di una vendita e' ``(ruolo, banda base,
   fase)``:
   - ``ruolo``: P/D/C/A;
   - ``banda base``: bucket [lo, hi) di ``base`` (PFC arrotondato), default
     ``<10, 10-25, 25-50, 50-100, 100-200, 200+``; configurable. Un valore
     sotto la prima banda cade nella prima, sopra l'ultima nell'ultima;
   - ``fase``: progresso delle vendite sul totale slot della lega
     (``teams x sum(slots)``, modalita' ``league`` — default) oppure sul
     progresso del ruolo (modalita' ``role``): ``start`` se progresso < 1/3,
     ``mid`` se in [1/3, 2/3], ``end`` se > 2/3. Se il denominatore non e'
     valido (slot zero/mancanti) si usa la posizione relativa nella
     sequenza osservata (fallback documentato).

2. **Pesi recency** — peso esponenziale continuo sull'indice evento
   (``i`` dell'evento TrendAuction; ``seq`` dello store; altrimenti la
   posizione nell'input): ``w_i = 2^((i - i_max)/half_life)``, cioe'
   ``exp(-(i_max - i) * ln2 / half_life)``. L'evento piu' recente ha peso 1,
   niente finestra rigida (default ``half_life = 25`` eventi).

3. **Centro e dispersione robusti** — mediana PONDERATA del ``premium_pfc``
   (l'outlier non sposta la mediana) e MAD ponderata come dispersione.

4. **Shrinkage gerarchico** — cella -> ruolo/fase -> ruolo -> globale, con
   peso ``w = n_eff / (n_eff + k)`` (``n_eff`` di Kish =
   ``(sum w)^2 / sum(w^2)``, default ``k = 10``):

       factor_global = clamp( w_global * center_global
                              + (1 - w_global) * 1.0 )
       factor_role   = clamp( w_role * center_role
                              + (1 - w_role) * factor_global )
       factor_rp     = clamp( w_rp * center_rp
                              + (1 - w_rp) * factor_role )
       factor_cell   = clamp( w_cell * center_cell
                              + (1 - w_cell) * factor_rp )

   Il ``prior`` esposto a ogni livello e' il fattore gia' shrunk del
   livello genitore (l'ancora dello shrinkage); ``raw_center`` e' la
   mediana ponderata grezza della cella. Con n piccolo il fattore resta
   vicino al prior; con n grande domina il dato.

5. **Bounds e confidenza** — ogni centro/fattore/prior e' clampato in
   ``[factor_min, factor_max]`` (default [0.5, 2.0]). CI =
   ``factor ± min(ci_z * MAD / sqrt(n_eff), ci_max_half_width)``
   (default z=1.96, meta-larghezza massima 0.5), clampata ai bound e
   contenente sempre ``factor``: piu' n_eff cresce, piu' la CI si
   restringe. ``confidence``: ``low`` (n_eff < 10), ``medium`` (10-29),
   ``high`` (>= 30). Con n = 0 la CI e' NEUTRA: ``factor ± ci_max_half_width``
   (default [0.5, 1.5] attorno al fattore neutro 1.0).

6. **Fallback** — ``estimate_player`` usa la cella corrente se ha dati,
   altrimenti ruolo/fase, ruolo, globale, poi neutro (factor 1.0). Il
   ``source`` esposto indica il livello effettivamente usato.

Contratto
---------
- ``CalibrationConfig``: parametri (bande crescenti, k>0, half_life>0,
  factor_min < factor_max, ci_max_half_width > 0, phase_mode league|role).
- ``parse_calibration_params(league_cfg) -> (CalibrationConfig, errors)``:
  legge le chiavi ``calibration_*`` della config di lega con i default.
- ``calibration_from_events(events, league_cfg, config=None) -> CalibrationReport``:
  report completo (fase, celle, n/CI); le vendite non attive (unsold/revoke)
  sono IGNORATE.
- ``CalibrationReport.to_dict()``: JSON serializzabile (report + fase +
  celle con n/CI; ``None`` = +inf nelle bande).
- ``estimate_player(report, p, current_phase, expected=None) -> dict``:
  advisory per il giocatore: ``factor``, ``expected_if_applied``, ``range``,
  ``applied=False``, ``reason="advisory_gate_off"``, metadata della cella.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeGuard

ROLE_ORDER = ("P", "D", "C", "A")
PHASE_START = "start"
PHASE_MID = "mid"
PHASE_END = "end"
PHASES = (PHASE_START, PHASE_MID, PHASE_END)

# Bande default: [lo, hi) — lo incluso, hi escluso; l'ultimo hi = +inf.
DEFAULT_PRICE_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 10.0),
    (10.0, 25.0),
    (25.0, 50.0),
    (50.0, 100.0),
    (100.0, 200.0),
    (200.0, math.inf),
)
DEFAULT_K = 10.0
DEFAULT_HALF_LIFE = 25.0
DEFAULT_FACTOR_MIN = 0.5
DEFAULT_FACTOR_MAX = 2.0
DEFAULT_CI_MAX_HALF_WIDTH = 0.5
DEFAULT_CI_Z = 1.96
DEFAULT_PHASE_MODE = "league"
PHASE_START_FRAC = 1 / 3
PHASE_END_FRAC = 2 / 3

# Chiavi del blocco advisory/config lette dalla config di lega (condivisa con
# league_config: validate li controlla quando presenti; qui stanno i default).
CALIBRATION_CONFIG_KEYS = (
    "calibration_price_bands",
    "calibration_k",
    "calibration_half_life",
    "calibration_factor_min",
    "calibration_factor_max",
    "calibration_ci_max_half_width",
    "calibration_phase_mode",
)

CONFIDENCE_HIGH_N = 30
CONFIDENCE_MEDIUM_N = 10


# ------------------------------------------------------------------- helpers
def _r4(x: float) -> float:
    return round(x, 4)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _is_number(x: Any) -> TypeGuard[float | int]:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """Mediana ponderata: valore col peso cumulato che raggiunge meta' totale."""
    order = sorted(range(len(values)), key=lambda j: (values[j], j))
    total = sum(weights)
    cum = 0.0
    for j in order:
        cum += weights[j]
        if cum >= total / 2:
            return values[j]
    return values[order[-1]]


def _n_eff(weights: list[float]) -> float:
    """Dimensione effettiva di Kish: (sum w)^2 / sum(w^2). Pari a n se uniformi."""
    den = sum(w * w for w in weights)
    return (sum(weights) ** 2) / den if den > 0 else 0.0


def _band_label(lo: float, hi: float) -> str:
    def fmt(x: float) -> str:
        try:
            return str(int(x)) if float(x).is_integer() else str(round(x, 2))
        except (TypeError, ValueError, OverflowError):
            return str(x)

    if lo <= 0:
        return f"<{fmt(hi)}"
    if hi == math.inf:
        return f"{fmt(lo)}+"
    return f"{fmt(lo)}-{fmt(hi)}"


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class CalibrationConfig:
    """Parametri della calibrazione (advisory-only, WP8).

    Validati da ``validate()``: bande crescenti (lo >= 0, lo < hi, non
    sovrapposte), ``k > 0``, ``half_life > 0``, ``ci_max_half_width > 0``,
    ``0 < factor_min < factor_max``, ``phase_mode`` in league|role e
    ``0 < phase_start < phase_end < 1``."""

    price_bands: tuple[tuple[float, float], ...] = DEFAULT_PRICE_BANDS
    k: float = DEFAULT_K
    half_life: float = DEFAULT_HALF_LIFE
    factor_min: float = DEFAULT_FACTOR_MIN
    factor_max: float = DEFAULT_FACTOR_MAX
    ci_max_half_width: float = DEFAULT_CI_MAX_HALF_WIDTH
    ci_z: float = DEFAULT_CI_Z
    phase_mode: str = DEFAULT_PHASE_MODE
    phase_start: float = PHASE_START_FRAC
    phase_end: float = PHASE_END_FRAC

    def validate(self) -> list[str]:
        """Errori di configurazione (lista vuota = ok). Non muta nulla."""
        errors: list[str] = []
        bands = self.price_bands
        if not bands:
            errors.append("calibration_price_bands: serve almeno una banda")
        for i, (lo, hi) in enumerate(bands):
            ok = (
                _is_number(lo)
                and lo >= 0
                and (hi == math.inf or _is_number(hi))
                and lo < hi
            )
            if not ok:
                errors.append(
                    f"calibration_price_bands[{i}]: banda non valida "
                    f"(serve lo >= 0 e lo < hi; hi puo' essere +inf solo sull'ultima)"
                    f" — trovato ({lo!r}, {hi!r})"
                )
        for i in range(1, len(bands)):
            if bands[i][0] < bands[i - 1][1]:
                errors.append(
                    "calibration_price_bands: bande non crescenti (si sovrappongono "
                    f"tra [{i - 1}] e [{i}])"
                )
        if not (_is_number(self.k) and self.k > 0):
            errors.append("calibration_k deve essere un numero > 0")
        if not (_is_number(self.half_life) and self.half_life > 0):
            errors.append("calibration_half_life deve essere un numero > 0")
        if not (_is_number(self.ci_max_half_width) and self.ci_max_half_width > 0):
            errors.append("calibration_ci_max_half_width deve essere un numero > 0")
        if not (
            _is_number(self.factor_min)
            and _is_number(self.factor_max)
            and 0 < self.factor_min < self.factor_max
        ):
            errors.append(
                "calibration_factor_min/max: serve 0 < factor_min < factor_max"
            )
        if self.phase_mode not in ("league", "role"):
            errors.append("calibration_phase_mode deve essere 'league' o 'role'")
        if not (
            _is_number(self.phase_start)
            and _is_number(self.phase_end)
            and 0 < self.phase_start < self.phase_end < 1
        ):
            errors.append(
                "soglie di fase invalide: serve 0 < phase_start < phase_end < 1"
            )
        return errors

    def to_dict(self) -> dict[str, Any]:
        """Config JSON-serializzabile (``None`` = +inf nell'ultima banda)."""
        return {
            "price_bands": [
                [_r4(lo), None if hi == math.inf else _r4(hi)]
                for lo, hi in self.price_bands
            ],
            "k": self.k,
            "half_life": self.half_life,
            "factor_min": self.factor_min,
            "factor_max": self.factor_max,
            "ci_max_half_width": self.ci_max_half_width,
            "ci_z": self.ci_z,
            "phase_mode": self.phase_mode,
            "phase_start": _r4(self.phase_start),
            "phase_end": _r4(self.phase_end),
        }


def _parse_bands(raw: Any, errors: list[str]) -> tuple[tuple[float, float], ...] | None:
    """Bande da input non tipizzato: lista di coppie [lo, hi] (None = +inf)."""
    if not isinstance(raw, (list, tuple)) or not raw:
        errors.append(
            "calibration_price_bands: serve una lista non vuota di coppie [lo, hi]"
        )
        return None
    out: list[tuple[float, float]] = []
    n = len(raw)
    for i, pair in enumerate(raw):
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            errors.append(f"calibration_price_bands[{i}]: serve una coppia [lo, hi]")
            return None
        lo = pair[0]
        hi = pair[1]
        if hi is None:
            if i != n - 1:
                errors.append(
                    f"calibration_price_bands[{i}]: hi = +inf (None) e' ammesso "
                    "solo sull'ultima banda"
                )
                return None
            hi = math.inf
        if not (
            isinstance(lo, (int, float))
            and not isinstance(lo, bool)
            and math.isfinite(lo)
        ):
            errors.append(
                f"calibration_price_bands[{i}]: estremo lo non numerico {lo!r}"
            )
            return None
        if not (
            isinstance(hi, (int, float))
            and not isinstance(hi, bool)
            and hi == math.inf
            or (
                isinstance(hi, (int, float))
                and not isinstance(hi, bool)
                and math.isfinite(hi)
                and hi > lo
            )
        ):
            errors.append(
                f"calibration_price_bands[{i}]: estremo hi non numerico o non > lo "
                f"({lo!r}, {hi!r})"
            )
            return None
        out.append((_to_float(lo, lo), _to_float(hi, hi)))
    return tuple(out)


def _to_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_float_key(
    league_cfg: dict[str, Any], key: str, default: float, errors: list[str]
) -> float:
    """Valore numerico di una chiave ``calibration_*`` (default se assente)."""
    raw = league_cfg.get(key)
    if raw is None:
        return default
    if not _is_number(raw):
        errors.append(f"{key} deve essere un numero (trovato {raw!r})")
        return default
    return _to_float(raw, default)


def parse_calibration_params(
    league_cfg: dict[str, Any] | None,
) -> tuple[CalibrationConfig, list[str]]:
    """Config di calibrazione dalla config di lega + errori di validazione.

    Legge le chiavi ``calibration_*`` (con i default documentati) e valida
    tutto (tipo, range e vincoli incrociati). Il flag
    ``use_calibration_in_price`` NON appartiene a CalibrationConfig: resta
    una chiave della config di lega, esposta ma non applicata in questo WP.
    """
    cfg = league_cfg or {}
    errors: list[str] = []
    bands = DEFAULT_PRICE_BANDS
    if cfg.get("calibration_price_bands") is not None:
        parsed = _parse_bands(cfg.get("calibration_price_bands"), errors)
        if parsed is not None:
            bands = parsed
    k = _parse_float_key(cfg, "calibration_k", DEFAULT_K, errors)
    half_life = _parse_float_key(
        cfg, "calibration_half_life", DEFAULT_HALF_LIFE, errors
    )
    fmin = _parse_float_key(cfg, "calibration_factor_min", DEFAULT_FACTOR_MIN, errors)
    fmax = _parse_float_key(cfg, "calibration_factor_max", DEFAULT_FACTOR_MAX, errors)
    ciw = _parse_float_key(
        cfg, "calibration_ci_max_half_width", DEFAULT_CI_MAX_HALF_WIDTH, errors
    )
    mode = cfg.get("calibration_phase_mode", DEFAULT_PHASE_MODE)
    if not isinstance(mode, str):
        errors.append("calibration_phase_mode deve essere 'league' o 'role'")
        mode = DEFAULT_PHASE_MODE
    config = CalibrationConfig(
        price_bands=bands,
        k=k,
        half_life=half_life,
        factor_min=fmin,
        factor_max=fmax,
        ci_max_half_width=ciw,
        phase_mode=mode,
    )
    errors += config.validate()
    return config, errors


def band_for_base(base: float, config: CalibrationConfig) -> str:
    """Banda [lo, hi) della base (etichetta): fuori dall'unione -> prima/ultima."""
    for lo, hi in config.price_bands:
        if lo <= base < hi:
            return _band_label(lo, hi)
    if base < config.price_bands[0][0]:
        return _band_label(*config.price_bands[0])
    return _band_label(*config.price_bands[-1])


def _phase_of(progress: float, config: CalibrationConfig) -> str:
    if progress <= config.phase_start:
        return PHASE_START
    if progress >= config.phase_end:
        return PHASE_END
    return PHASE_MID


def total_slots(league_cfg: dict[str, Any] | None) -> int:
    """Slot totali della lega = teams x sum(slots[ruolo]); 0 se non derivabile."""
    cfg = league_cfg or {}
    teams = cfg.get("teams")
    slots = cfg.get("slots")
    if not (isinstance(teams, int) and teams > 0 and isinstance(slots, dict)):
        return 0
    return teams * sum(max(0, _to_int(slots.get(r, 0), 0)) for r in ROLE_ORDER)


# ------------------------------------------------------------------ evento
def _normalize_event(e: dict[str, Any], position: int) -> dict[str, Any] | None:
    """Evento -> dict normalizzato (ruolo, base, premium_pfc, idx) oppure None.

    Accetta sia gli eventi ``TrendAuction`` (chiave ``kind``, ``premium_pfc``)
    sia gli eventi del log store (chiave ``type``/``payload``). Gli eventi
    NON vendita (unsold/revoke/league_configured) sono ignorati (None)."""
    kind = e.get("kind")
    typ = e.get("type")
    raw_payload = e.get("payload")
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    payload: dict[str, Any] = raw_payload
    if kind not in (None, "sold") and typ not in (None, "sold"):
        return None
    if kind == "unsold" or typ in ("unsold", "revoke", "league_configured"):
        return None
    role_raw: Any = e.get("ruolo")
    if not isinstance(role_raw, str):
        role_raw = payload.get("ruolo")
    role = role_raw if isinstance(role_raw, str) else None
    if role not in ROLE_ORDER:
        return None
    base_raw: Any = e.get("base")
    if base_raw is None:
        base_raw = payload.get("base")
    base = base_raw
    price_raw: Any = e.get("price")
    if price_raw is None:
        price_raw = payload.get("price")
    premium_raw: Any = e.get("premium_pfc")
    if premium_raw is None:
        premium_raw = payload.get("premium_pfc")
    if not _is_number(premium_raw):
        # Fallback documentato: premium = price / base se assenti ma derivabili.
        if _is_number(price_raw) and _is_number(base_raw) and base_raw > 0:
            premium = price_raw / base_raw
        else:
            return None
    else:
        premium = premium_raw
    idx_raw: Any = e.get("i")
    if not _is_number(idx_raw):
        idx_raw = e.get("seq")
    if not _is_number(idx_raw):
        idx = _to_float(position, position)
    else:
        idx = _to_float(idx_raw, position)
    if not _is_number(base) or not _is_number(premium):
        return None
    if base <= 0 or premium <= 0:
        return None
    return {
        "role": role,
        "base": _to_float(base, 0.0),
        "premium": _to_float(premium, 0.0),
        "idx": idx,
    }


# ------------------------------------------------------------------- stats
@dataclass(frozen=True)
class LevelStats:
    """Statistiche di un livello della gerarchia (globale/ruolo/fase/cella).

    ``raw_center`` e ``mad`` sono la mediana ponderata e la MAD grezze (None
    senza dati); ``prior`` e' il fattore del livello genitore (gia' shrunk);
    ``factor`` e' il fattore finale (shrunk e clampato); ``ci_lo/ci_hi`` la
    confidenza bounded contenente sempre ``factor``; ``confidence`` low/
    medium/high su n_eff; ``nodata`` True se n = 0."""

    n: int
    n_eff: float
    center: float | None
    mad: float | None
    prior: float
    factor: float
    ci_lo: float
    ci_hi: float
    ci_half_width: float
    confidence: str
    nodata: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "n_eff": round(self.n_eff, 2),
            "raw_center": _r4(self.center) if self.center is not None else None,
            "mad": _r4(self.mad) if self.mad is not None else None,
            "prior": _r4(self.prior),
            "factor": _r4(self.factor),
            "ci_lo": _r4(self.ci_lo),
            "ci_hi": _r4(self.ci_hi),
            "ci_half_width": _r4(self.ci_half_width),
            "confidence": self.confidence,
            "nodata": self.nodata,
        }


@dataclass(frozen=True)
class CellStats(LevelStats):
    """LevelStats + identita' della cella (ruolo, banda, fase)."""

    role: str
    band: str
    phase: str

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.update({"role": self.role, "band": self.band, "phase": self.phase})
        return out


def _level_stats(
    n: int,
    n_eff: float,
    center: float | None,
    mad: float | None,
    prior: float,
    config: CalibrationConfig,
) -> LevelStats:
    """Fattore shrinkato + CI bounded + confidence per un livello.

    Con n > 0: ``factor = clamp(w * center + (1 - w) * prior)`` con
    ``w = n_eff/(n_eff+k)`` (center prima clampato ai bound), CI =
    ``factor ± min(ci_z*MAD/sqrt(n_eff), ci_max_half_width)``. Con n = 0:
    fattore = prior (clampato), CI neutra ``factor ± ci_max_half_width``.

    I valori restano a PRECISIONE PIENA: l'arrotondamento avviene SOLO
    nella serializzazione (``to_dict()`` / ``estimate_player``), mai nella
    gerarchia (il prior di ogni livello e' il fattore intero del genitore)."""
    nodata = n == 0
    if nodata:
        factor = _clamp(prior, config.factor_min, config.factor_max)
        half = config.ci_max_half_width
    else:
        w = n_eff / (n_eff + config.k)
        raw = center if center is not None else prior
        factor = _clamp(w * raw + (1 - w) * prior, config.factor_min, config.factor_max)
        half = 0.0
        if mad is not None and n_eff > 0:
            half = min(
                config.ci_z * mad / math.sqrt(n_eff),
                config.ci_max_half_width,
            )
    ci_lo = _clamp(factor - half, config.factor_min, config.factor_max)
    ci_hi = _clamp(factor + half, config.factor_min, config.factor_max)
    ci_lo = min(ci_lo, factor)
    ci_hi = max(ci_hi, factor)
    if n_eff >= CONFIDENCE_HIGH_N:
        confidence = "high"
    elif n_eff >= CONFIDENCE_MEDIUM_N:
        confidence = "medium"
    else:
        confidence = "low"
    return LevelStats(
        n=n,
        n_eff=n_eff,
        center=center,
        mad=mad,
        prior=prior,
        factor=factor,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        ci_half_width=(ci_hi - ci_lo) / 2,
        confidence=confidence,
        nodata=nodata,
    )


def _group_stats(
    group: list[dict[str, Any]],
) -> tuple[int, float, float | None, float | None]:
    """n, n_eff (Kish sui pesi recency), mediana ponderata, MAD ponderata."""
    n = len(group)
    if n == 0:
        return 0, 0.0, None, None
    vals = [g["premium"] for g in group]
    ws = [g["w"] for g in group]
    eff = _n_eff(ws)
    center = _weighted_median(vals, ws)
    mad = _weighted_median([abs(v - center) for v in vals], ws)
    return n, eff, center, mad


# ------------------------------------------------------------------- report
@dataclass(frozen=True)
class CalibrationReport:
    """Report completo della calibrazione (advisory, WP8).

    Attributi: ``config``, ``nodata``, ``n_sales``, ``total_slots``,
    ``phase_global``, ``phase_roles``, ``global_stats``, ``roles``,
    ``role_phases`` (chiavi (ruolo, fase)), ``cells`` (chiavi
    (ruolo, banda, fase)). ``to_dict()`` serializza in JSON."""

    config: CalibrationConfig
    nodata: bool
    n_sales: int
    total_slots: int
    phase_global: str
    phase_roles: dict[str, str]
    global_stats: LevelStats
    roles: dict[str, LevelStats]
    role_phases: dict[tuple[str, str], LevelStats]
    cells: dict[tuple[str, str, str], CellStats]

    def current_phase_for(self, role: str) -> str:
        """Fase corrente per il ruolo (in modalita' ``league`` e' la fase globale)."""
        return self.phase_roles.get(role, self.phase_global)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodata": self.nodata,
            "n_sales": self.n_sales,
            "total_slots": self.total_slots,
            "phase": {
                "global": self.phase_global,
                "roles": dict(self.phase_roles),
                "mode": self.config.phase_mode,
                "start": _r4(self.config.phase_start),
                "end": _r4(self.config.phase_end),
            },
            "config": self.config.to_dict(),
            "global": self.global_stats.to_dict(),
            "roles": {r: s.to_dict() for r, s in self.roles.items()},
            "role_phases": {
                f"{r}|{ph}": s.to_dict() for (r, ph), s in self.role_phases.items()
            },
            "cells": {
                f"{r}|{b}|{ph}": s.to_dict() for (r, b, ph), s in self.cells.items()
            },
        }


def calibration_from_events(
    events: Iterable[dict[str, Any]],
    league_cfg: dict[str, Any] | None = None,
    config: CalibrationConfig | None = None,
) -> CalibrationReport:
    """Calibrazione adattiva dalle vendite attive (advisory-only).

    ``events``: eventi ``sold`` attivi di TrendAuction/replay (o eventi del
    log store con ``payload``); unsold/revoke sono ignorati. ``league_cfg``:
    config normalizzata della lega (teams, slots e chiavi ``calibration_*``).
    Ritorna un ``CalibrationReport`` deterministico (nessuna randomicità)."""
    if config is None:
        config, _ = parse_calibration_params(league_cfg)

    # Eventi ATTIVI: esclude le vendite revocate (il cui ``seq`` compare in un
    # ``supersedes`` di un evento ``revoke``) e i marcatori non-vendita.
    superseded: set[int] = set()
    for e in events:
        try:
            sup = e.get("supersedes")
            if sup is not None:
                superseded.add(int(sup))
        except (TypeError, ValueError):
            continue

    def _seq_of(ev: dict[str, Any]) -> int | None:
        raw = ev.get("seq")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return None

    active = [
        ev for ev in events if _seq_of(ev) is None or _seq_of(ev) not in superseded
    ]

    sales = [
        norm
        for position, ev in enumerate(active)
        if (norm := _normalize_event(ev, position)) is not None
    ]
    n_sales = len(sales)
    total = total_slots(league_cfg)

    # Pesi recency esponenziali continui sull'indice evento (niente finestra).
    if sales:
        imax = max(s["idx"] for s in sales)
        for s in sales:
            s["w"] = math.exp(-(imax - s["idx"]) * math.log(2) / config.half_life)

    # Fase per evento: progresso delle vendite sul totale slot (deterministico).
    ordered = sorted(sales, key=lambda s: s["idx"])
    role_so_far: dict[str, int] = dict.fromkeys(ROLE_ORDER, 0)
    for j, s in enumerate(ordered):
        if config.phase_mode == "role":
            denom = _role_slots(league_cfg, s["role"])
        else:
            denom = total
        if denom > 0:
            progress = (j + 1) / denom
        else:
            # fallback documentato: posizione relativa nella sequenza osservata
            progress = (j + 1) / n_sales if n_sales else 0.0
        s["phase"] = _phase_of(progress, config)
        role_so_far[s["role"]] += 1

    # Raggruppamenti per livello della gerarchia.
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for s in sales:
        groups.setdefault(("global",), []).append(s)
        groups.setdefault(("role", s["role"]), []).append(s)
        groups.setdefault(("rp", s["role"], s["phase"]), []).append(s)
        band = band_for_base(s["base"], config)
        groups.setdefault(("cell", s["role"], band, s["phase"]), []).append(s)

    def _stats(key: tuple[Any, ...]) -> tuple[int, float, float | None, float | None]:
        return _group_stats(groups.get(key, []))

    # Bottom-up: globale -> ruolo -> ruolo/fase -> cella (ogni prior = il
    # fattore gia' shrunk del genitore; la radice ha prior neutro 1.0).
    g_n, g_eff, g_center, g_mad = _stats(("global",))
    global_stats = _level_stats(g_n, g_eff, g_center, g_mad, 1.0, config)

    roles: dict[str, LevelStats] = {}
    for role in ROLE_ORDER:
        n, eff, c, mad = _stats(("role", role))
        roles[role] = _level_stats(n, eff, c, mad, global_stats.factor, config)

    role_phases: dict[tuple[str, str], LevelStats] = {}
    for role in ROLE_ORDER:
        for ph in PHASES:
            n, eff, c, mad = _stats(("rp", role, ph))
            role_phases[(role, ph)] = _level_stats(
                n, eff, c, mad, roles[role].factor, config
            )

    cells: dict[tuple[str, str, str], CellStats] = {}
    for key, group in groups.items():
        if key[0] != "cell":
            continue
        _, role, band, ph = key
        n, eff, c, mad = _group_stats(group)
        base = _level_stats(n, eff, c, mad, role_phases[(role, ph)].factor, config)
        cells[(role, band, ph)] = CellStats(
            n=base.n,
            n_eff=base.n_eff,
            center=base.center,
            mad=base.mad,
            prior=base.prior,
            factor=base.factor,
            ci_lo=base.ci_lo,
            ci_hi=base.ci_hi,
            ci_half_width=base.ci_half_width,
            confidence=base.confidence,
            nodata=base.nodata,
            role=role,
            band=band,
            phase=ph,
        )

    # Fase corrente (stato attuale della lega, per ruolo e globale).
    prog_global = n_sales / total if total > 0 else 0.0
    phase_global = _phase_of(prog_global, config)
    phase_roles: dict[str, str] = {}
    for role in ROLE_ORDER:
        if config.phase_mode == "role":
            denom = _role_slots(league_cfg, role)
            cnt = sum(1 for s in sales if s["role"] == role)
            prog = cnt / denom if denom > 0 else prog_global
            phase_roles[role] = _phase_of(prog, config)
        else:
            phase_roles[role] = phase_global

    return CalibrationReport(
        config=config,
        nodata=n_sales == 0,
        n_sales=n_sales,
        total_slots=total,
        phase_global=phase_global,
        phase_roles=phase_roles,
        global_stats=global_stats,
        roles=roles,
        role_phases=role_phases,
        cells=cells,
    )


def _role_slots(league_cfg: dict[str, Any] | None, role: str) -> int:
    cfg = league_cfg or {}
    teams = cfg.get("teams")
    slots = cfg.get("slots")
    if not (isinstance(teams, int) and teams > 0 and isinstance(slots, dict)):
        return 0
    raw = slots.get(role, 0)
    if not (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
        return 0
    try:
        return teams * max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ advisory
def estimate_player(
    report: CalibrationReport,
    p: dict[str, Any],
    current_phase: str | None = None,
    expected: float | None = None,
) -> dict[str, Any]:
    """Advisory di calibrazione per il giocatore ``p`` (advisory-only).

    Usa la cella corrente ``(ruolo, banda base, fase)`` se ha dati,
    altrimenti fallback gerarchico: ruolo/fase -> ruolo -> globale -> neutro
    (``source`` espone il livello usato). ``current_phase`` default = fase
    corrente del report per il ruolo. ``expected`` = prezzo di mercato atteso
    live (se assente si usa ``p["base"]``) per ``expected_if_applied`` e
    ``range``. NON modifica nessun blocco di prezzo: ritorna
    ``applied=False``, ``reason="advisory_gate_off"``. Con zero dati:
    ``nodata=True``, factor 1.0, CI neutra [0.5, 1.5] (default)."""
    config = report.config
    role = str(p.get("ruolo") or "").strip().upper()
    base = p.get("base")
    band = None
    if _is_number(base) and base > 0:
        band = band_for_base(base, config)
    if current_phase is None:
        current_phase = report.current_phase_for(role)

    cell = report.cells.get((role, band, current_phase)) if band is not None else None
    if cell is not None and not cell.nodata:
        stats: LevelStats = cell
        source = "cell"
    else:
        rp = report.role_phases.get((role, current_phase))
        if rp is not None and not rp.nodata:
            stats = rp
            source = "role_phase"
        else:
            rl = report.roles.get(role)
            if rl is not None and not rl.nodata:
                stats = rl
                source = "role"
            elif not report.global_stats.nodata:
                stats = report.global_stats
                source = "global"
            else:
                stats = report.global_stats
                source = "nodata"

    expected_base = base if not _is_number(expected) else expected
    exp_applied: int | None = None
    rng: dict[str, int] | None = None
    if _is_number(expected_base) and expected_base > 0:
        exp_applied = max(1, round(expected_base * stats.factor))
        rng_lo = max(1, min(round(expected_base * stats.ci_lo), exp_applied))
        rng_hi = max(round(expected_base * stats.ci_hi), exp_applied)
        rng = {"lo": rng_lo, "hi": rng_hi}

    return {
        "factor": _r4(stats.factor),
        "expected_if_applied": exp_applied,
        "range": rng,
        "applied": False,
        "reason": "advisory_gate_off",
        "cell": {"role": role, "band": band, "phase": current_phase},
        "source": source,
        "nodata": stats.nodata,
        "n": stats.n,
        "n_eff": round(stats.n_eff, 2),
        "mad": _r4(stats.mad) if stats.mad is not None else None,
        "raw_center": _r4(stats.center) if stats.center is not None else None,
        "prior": _r4(stats.prior),
        "ci_lo": _r4(stats.ci_lo),
        "ci_hi": _r4(stats.ci_hi),
        "ci_half_width": _r4(stats.ci_half_width),
        "confidence": stats.confidence,
    }
