"""Test del gate di qualità (WP9): soglie, verdict puri, metriche.

Casi sintetici ai confini: pass/no-pass esatti sulle soglie, mai pass su
NaN/n insufficiente/leakage, metriche non computabili = None (mai NaN).
"""

import gates  # pyright: ignore[reportMissingImports]
import pytest  # pyright: ignore[reportMissingImports]


def test_gate_pass_con_dati_sufficienti():
    v = gates.evaluate_gate(n=30, mape_model=0.10, mape_baseline=0.20, spearman=0.50)
    assert v.passed
    assert v.reasons == ()
    assert v.checks["mape_improvement"] == pytest.approx(0.5)


def test_gate_fail_sotto_minimo_campione():
    v = gates.evaluate_gate(n=29, mape_model=0.10, mape_baseline=0.20, spearman=0.50)
    assert not v.passed
    assert any("campione insufficiente" in r for r in v.reasons)


def test_gate_confine_campione_esatto():
    v = gates.evaluate_gate(n=30, mape_model=0.10, mape_baseline=0.20, spearman=0.30)
    assert v.passed


def test_gate_confine_miglioramento_insufficiente():
    # miglioramento 4% < 5%: fail; 5% esatto: pass
    v = gates.evaluate_gate(n=30, mape_model=0.96, mape_baseline=1.0, spearman=0.5)
    assert not v.passed
    assert any("miglioramento MAPE insufficiente" in r for r in v.reasons)
    v = gates.evaluate_gate(n=30, mape_model=0.95, mape_baseline=1.0, spearman=0.5)
    assert v.passed


def test_gate_confine_spearman():
    v = gates.evaluate_gate(n=30, mape_model=0.1, mape_baseline=0.2, spearman=0.29)
    assert not v.passed
    assert any("Spearman out-of-sample insufficiente" in r for r in v.reasons)
    v = gates.evaluate_gate(n=30, mape_model=0.1, mape_baseline=0.2, spearman=0.30)
    assert v.passed


def test_gate_mai_pass_con_nan():
    v = gates.evaluate_gate(
        n=100, mape_model=float("nan"), mape_baseline=0.2, spearman=0.5
    )
    assert not v.passed
    v = gates.evaluate_gate(
        n=100, mape_model=0.1, mape_baseline=float("inf"), spearman=0.5
    )
    assert not v.passed
    v = gates.evaluate_gate(
        n=100, mape_model=0.1, mape_baseline=0.2, spearman=float("nan")
    )
    assert not v.passed
    assert any("non validi" in r or "non valido" in r for r in v.reasons)


def test_gate_mai_pass_con_metriche_assenti():
    v = gates.evaluate_gate(n=100, mape_model=None, mape_baseline=None, spearman=None)
    assert not v.passed
    assert len(v.reasons) == 2  # MAPE + Spearman


def test_gate_mai_pass_con_leakage():
    v = gates.evaluate_gate(
        n=100, mape_model=0.1, mape_baseline=0.2, spearman=0.5, leakage=True
    )
    assert not v.passed
    assert any("leakage" in r for r in v.reasons)


def test_gate_extra_reasons_bloccano_sempre():
    v = gates.evaluate_gate(
        n=100,
        mape_model=0.01,
        mape_baseline=1.0,
        spearman=0.9,
        extra_reasons=("rendimento proxy",),
    )
    assert not v.passed
    assert "rendimento proxy" in v.reasons


def test_gate_require_mape_false_salta_solo_se_entrambi_assenti():
    v = gates.evaluate_gate(
        n=100, mape_model=None, mape_baseline=None, spearman=0.5, require_mape=False
    )
    assert v.passed
    v = gates.evaluate_gate(
        n=100, mape_model=0.1, mape_baseline=None, spearman=0.5, require_mape=False
    )
    assert not v.passed  # una sola MAPE presente: fallimento esplicito


def test_gate_config_default_conservativi():
    cfg = gates.DEFAULT_GATE_CONFIG
    assert cfg.min_sample == 30
    assert cfg.mape_improvement_min == pytest.approx(0.05)
    assert cfg.spearman_min == pytest.approx(0.30)


def test_gate_config_validazione():
    assert gates.GateConfig().validate() == []
    errs = gates.GateConfig(
        min_sample=0, mape_improvement_min=1.5, spearman_min=2
    ).validate()
    assert len(errs) == 3


def test_gate_configurabile():
    cfg = gates.GateConfig(min_sample=5, mape_improvement_min=0.01, spearman_min=0.1)
    v = gates.evaluate_gate(
        n=5, mape_model=0.10, mape_baseline=0.102, spearman=0.15, config=cfg
    )
    assert v.passed
    v = gates.evaluate_gate(
        n=4, mape_model=0.10, mape_baseline=0.20, spearman=0.5, config=cfg
    )
    assert not v.passed


# ---------------------------------------------------------------- metriche
def test_metriche_base():
    assert gates.mae([4, 2], [3, 3]) == pytest.approx(1.0)
    assert gates.rmse([4, 2], [3, 3]) == pytest.approx(1.0)
    assert gates.mape([100, 200], [110, 240]) == pytest.approx((0.1 + 0.2) / 2)
    assert gates.coverage([10, 11, 20], [10, 10, 10]) == pytest.approx(2 / 3)


def test_metriche_serie_vuota_none_mai_nan():
    assert gates.mae([], []) is None
    assert gates.rmse([], []) is None
    assert gates.mape([], []) is None
    assert gates.spearman([], []) is None
    # coppia con NaN/inf scartata: serie di fatto vuota -> None (mai NaN)
    assert gates.mae([float("nan")], [1.0]) is None
    # inf scartata -> resta un'unica coppia: Spearman indefinito -> None
    assert gates.spearman([float("inf"), 2.0], [1.0, 2.0]) is None


def test_metriche_lunghezze_diverse_errore():
    with pytest.raises(ValueError):
        gates.mae([1.0], [1.0, 2.0])


def test_spearman_perfetto_e_inverso():
    assert gates.spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert gates.spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_tie_medi():
    # tie gestiti con rank medio: x costante -> None (indefinito)
    assert gates.spearman([1, 1, 1], [1, 2, 3]) is None
    # y costante -> None
    assert gates.spearman([1, 2, 3], [5, 5, 5]) is None
    # n < 2 -> None
    assert gates.spearman([1.0], [2.0]) is None
    # valori non numerici scartati a coppie
    assert gates.spearman([1, float("nan"), 3], [10, 20, 30]) == pytest.approx(1.0)


def test_mape_esclude_actual_non_positivi():
    # actual <= 0 esclusi dal MAPE (indefinito), mai contaminazione
    assert gates.mape([0, 100], [50, 110]) == pytest.approx(0.1)
    assert gates.mape([0], [50]) is None


def test_verdict_to_dict_serializzabile():
    v = gates.evaluate_gate(n=10, mape_model=0.2, mape_baseline=0.2, spearman=0.1)
    d = v.to_dict()
    assert d["passed"] is False
    assert isinstance(d["reasons"], list)
    assert "mape_improvement" in d["checks"]
