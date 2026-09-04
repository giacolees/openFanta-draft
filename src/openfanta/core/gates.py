#!/usr/bin/env -S uv run --quiet python
"""Gate di qualità per i backtest (WP9): soglie esplicite e verdict puri.

Il gate è il **criterio binario** che decide se un modello (calibrazione del
mercato, ranking fantasy) è abbastanza buono da meritare una promozione.
Il verdetto NON attiva nulla da solo: produce solo ``passed``/``reasons``;
l'attivazione effettiva (``use_calibration_in_price``) richiede una decisione
esplicita futura e resta di default False.

Soglie default conservative (overridabili):

- ``min_sample`` = 30: numero minimo di osservazioni out-of-sample;
- ``mape_improvement_min`` = 0.05: il modello deve battere la MIGLIORE
  baseline di almeno il 5% in MAPE relativo;
- ``spearman_min`` = 0.30: correlazione di Spearman out-of-sample minima.

Il gate **non passa mai** con:

- ``n`` insufficiente (n < ``min_sample``);
- metriche non valide (NaN/inf/None — un metrico assente è un fallimento,
  non un salto del controllo);
- flag di leakage (informazioni future usate nella predizione).

Tutte le funzioni sono pure: nessun IO, nessuna global state, determinismo
totale. Gli helper di metrica (MAE/RMSE/MAPE/Spearman/coverage) vivono qui
così i due backtest condividono esattamente le stesse definizioni.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

# ---------------------------------------------------------------- costanti
MIN_SAMPLE_N = 30
MAPE_IMPROVEMENT_MIN = 0.05
SPEARMAN_MIN = 0.30

# Coverage default: il prezzo reale cade dentro [pred * LO, pred * HI].
COVERAGE_LO = 0.9
COVERAGE_HI = 1.1

# Rounding dei numeri esposti nei report.
ROUND = 4


def _r4(x: float) -> float:
    return round(x, ROUND)


def _finite(x: Any) -> TypeGuard[float]:
    """Type guard: True se ``x`` e' un numero (non bool) finito."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return math.isfinite(x)


# ------------------------------------------------------------------ config
@dataclass(frozen=True)
class GateConfig:
    """Soglie del gate. ``validate()`` ritorna errori (lista vuota = ok)."""

    min_sample: int = MIN_SAMPLE_N
    mape_improvement_min: float = MAPE_IMPROVEMENT_MIN
    spearman_min: float = SPEARMAN_MIN

    def validate(self) -> list[str]:
        errors: list[str] = []
        if type(self.min_sample) is not int or self.min_sample < 1:
            errors.append("min_sample deve essere un intero >= 1")
        if not (
            _finite(self.mape_improvement_min) and 0 <= self.mape_improvement_min < 1
        ):
            errors.append("mape_improvement_min deve essere in [0, 1)")
        if not (_finite(self.spearman_min) and -1 <= self.spearman_min <= 1):
            errors.append("spearman_min deve essere in [-1, 1]")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_sample": self.min_sample,
            "mape_improvement_min": self.mape_improvement_min,
            "spearman_min": self.spearman_min,
        }


DEFAULT_GATE_CONFIG = GateConfig()


# ----------------------------------------------------------------- verdict
@dataclass(frozen=True)
class GateVerdict:
    """Esito del gate: ``passed`` sse ``reasons`` e' vuota.

    ``reasons`` e' una lista di stringhe umane (una per fallimento);
    ``checks`` espone il dettaglio numerico di ogni controllo."""

    passed: bool
    reasons: tuple[str, ...]
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "checks": self.checks,
        }


def evaluate_gate(
    n: int,
    mape_model: float | None = None,
    mape_baseline: float | None = None,
    spearman: float | None = None,
    leakage: bool = False,
    extra_reasons: Sequence[str] = (),
    config: GateConfig | None = None,
    require_mape: bool = True,
) -> GateVerdict:
    """Verdict del gate di qualità (funzione pura, mai side-effect).

    Parametri:

    - ``n``: campione out-of-sample delle predizioni del modello;
    - ``mape_model`` / ``mape_baseline``: MAPE del modello e della MIGLIORE
      baseline (entrambi frazioni, es. 0.18). Se entrambi presenti, il
      miglioramento relativo ``1 - model/baseline`` deve essere >= soglia;
      se solo uno e' presente (o non valido) il controllo fallisce;
    - ``spearman``: correlazione di Spearman out-of-sample del modello
      (il ranking deve essere almeno debolmente allineato al reale);
    - ``leakage``: True se e' stata usata informazione futura o in-sample;
    - ``extra_reasons``: motivi addizionali del chiamante (es. proxy,
      stessa stagione) che rendono il pass impossibile;
    - ``require_mape``: False per i gate che non confrontano MAPE (es. il
      rendimento stagionale, valutato su correlazione e non su errore di
      prezzo): con entrambe le MAPE assenti il controllo viene saltato;
      una sola presente resta comunque un fallimento esplicito.

    Il gate non passa MAI con n insufficiente, metriche non finite
    (NaN/inf/None) o leakage: un fallimento e' sempre esplicito e spiegato.
    """
    cfg = config or DEFAULT_GATE_CONFIG
    reasons: list[str] = []
    checks: dict[str, Any] = {}

    # ---- campione ----
    n_ok = isinstance(n, int) and not isinstance(n, bool) and n >= cfg.min_sample
    checks["n"] = n
    checks["min_sample"] = cfg.min_sample
    if not n_ok:
        reasons.append(f"campione insufficiente: n={n} < min_sample={cfg.min_sample}")

    # ---- leakage ----
    checks["leakage"] = bool(leakage)
    if leakage:
        reasons.append("leakage rilevato: informazione futura o in-sample")

    # ---- motivo addizionali del chiamante (proxy, stessa stagione, ...) ----
    for reason in extra_reasons:
        if reason and reason not in reasons:
            reasons.append(str(reason))

    # ---- MAPE vs migliore baseline ----
    # Con require_mape=False e entrambe le MAPE assenti il controllo viene
    # saltato (gate su sola correlazione); in tutti gli altri casi una MAPE
    # mancante/non valida e' un fallimento esplicito.
    mape_skip = not require_mape and mape_model is None and mape_baseline is None
    if mape_skip:
        checks["mape_model"] = None
        checks["mape_baseline"] = None
        checks["mape_improvement"] = None  # controllo saltato per design
    elif _finite(mape_model) and _finite(mape_baseline):
        m, b = mape_model, mape_baseline
        if m < 0 or b <= 0:
            reasons.append(
                f"MAPE non confrontabili: model={m!r}, baseline={b!r} "
                "(attesi frazioni >= 0 con baseline > 0)"
            )
            checks["mape_improvement"] = None
        else:
            improvement = 1.0 - m / b
            checks["mape_model"] = _r4(m)
            checks["mape_baseline"] = _r4(b)
            checks["mape_improvement"] = _r4(improvement)
            checks["mape_improvement_min"] = cfg.mape_improvement_min
            if improvement < cfg.mape_improvement_min:
                reasons.append(
                    f"miglioramento MAPE insufficiente: {improvement:.4f} < "
                    f"{cfg.mape_improvement_min} (model={m:.4f} vs "
                    f"baseline={b:.4f})"
                )
    else:
        checks["mape_model"] = mape_model
        checks["mape_baseline"] = mape_baseline
        reasons.append(
            "MAPE del modello o della baseline assenti/non validi "
            f"(model={mape_model!r}, baseline={mape_baseline!r})"
        )

    # ---- Spearman out-of-sample ----
    checks["spearman"] = spearman
    checks["spearman_min"] = cfg.spearman_min
    if _finite(spearman):
        s = spearman
        if s < cfg.spearman_min:
            reasons.append(
                f"Spearman out-of-sample insufficiente: {s:.4f} < {cfg.spearman_min}"
            )
    else:
        reasons.append(
            f"Spearman out-of-sample non valido: {spearman!r} "
            "(NaN/None = ranking non computabile)"
        )

    # Dedup preservando l'ordine: lo stesso motivo non appare due volte.
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return GateVerdict(passed=not seen, reasons=tuple(seen), checks=checks)


# ------------------------------------------------------------------ metriche
def mae(actuals: Sequence[float], preds: Sequence[float]) -> float | None:
    """Mean Absolute Error; None se input vuoto o non numerico."""
    pairs = _pairs(actuals, preds)
    if not pairs:
        return None
    return sum(abs(a - p) for a, p in pairs) / len(pairs)


def rmse(actuals: Sequence[float], preds: Sequence[float]) -> float | None:
    """Root Mean Squared Error; None se input vuoto o non numerico."""
    pairs = _pairs(actuals, preds)
    if not pairs:
        return None
    return math.sqrt(sum((a - p) ** 2 for a, p in pairs) / len(pairs))


def mape(actuals: Sequence[float], preds: Sequence[float]) -> float | None:
    """Mean Absolute Percentage Error (frazione, es. 0.18 = 18%).

    Coppie con ``actual <= 0`` sono escluse (MAPE indefinito); se ne
    rimane nessuna il risultato e' None (mai NaN implicito)."""
    pairs = [(a, p) for a, p in _pairs(actuals, preds) if a > 0]
    if not pairs:
        return None
    return sum(abs(a - p) / a for a, p in pairs) / len(pairs)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Correlazione di Spearman (rank con tie rotti per media).

    None se: n < 2, input non numerico, o una delle due serie e' costante
    (varianza dei rank zero: correlazione indefinita, mai un numero finto)."""
    pairs = _pairs(xs, ys)
    n = len(pairs)
    if n < 2:
        return None
    rx = _ranks([x for x, _ in pairs])
    ry = _ranks([y for _, y in pairs])
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def coverage(
    actuals: Sequence[float],
    preds: Sequence[float],
    lo: float = COVERAGE_LO,
    hi: float = COVERAGE_HI,
) -> float | None:
    """Frazione di prezzi reali dentro [pred*lo, pred*hi]; None se vuoto."""
    pairs = _pairs(actuals, preds)
    if not pairs:
        return None
    hits = sum(1 for a, p in pairs if a >= p * lo and a <= p * hi)
    return hits / len(pairs)


# ------------------------------------------------------------------- helpers
def _pairs(
    actuals: Sequence[float], preds: Sequence[float]
) -> list[tuple[float, float]]:
    """Coppie (actual, pred) valide: stessa lunghezza, valori numerici finiti.
    Un singolo valore non valido scarta l'INTERA coppia (mai contaminazione)."""
    if len(actuals) != len(preds):
        raise ValueError(f"serie di lunghezza diversa: {len(actuals)} vs {len(preds)}")
    out: list[tuple[float, float]] = []
    for a, p in zip(actuals, preds, strict=True):
        if _finite(a) and _finite(p):
            out.append((a, p))
    return out


def _ranks(values: list[float]) -> list[float]:
    """Rank 1-based con tie = media dei ranghi (deterministico)."""
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1  # ranghi i+1..j+1 -> media
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks
