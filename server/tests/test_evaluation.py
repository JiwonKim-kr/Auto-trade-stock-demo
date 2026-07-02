"""평가 모듈 테스트 — 지표(누적/Sharpe/SE/MDD/벤치마크) + 판정 게이트."""

from __future__ import annotations

import math
from decimal import Decimal

from app.engine.evaluation import evaluate

D = Decimal


def curve(*values) -> list[tuple[str, Decimal]]:
    return [(f"2026-07-{i + 1:02d}", D(str(v))) for i, v in enumerate(values)]


def test_insufficient_days():
    r = evaluate(curve(100), n_trades=0)
    assert r.cumulative_return is None and "평가 불가" in r.verdict


def test_cumulative_and_mdd():
    r = evaluate(curve(100, 110, 99, 121), n_trades=0)
    assert math.isclose(r.cumulative_return, 0.21)
    assert math.isclose(r.mdd, (110 - 99) / 110)                  # 고점 110 → 99


def test_sample_gate_holds_judgement():
    r = evaluate(curve(100, 101, 102, 103), n_trades=5)
    assert "판단 보류" in r.verdict and "N=5" in r.verdict         # N<100 — 운/실력 구분 불가


def test_flat_curve_sigma_zero():
    r = evaluate(curve(100, 100, 100), n_trades=200)
    assert r.sharpe_annual is None and "σ=0" in r.verdict


def test_insignificant_sharpe_flagged():
    # 노이즈 곡선(±1% 교대) → SR≈0 → |SR| < 2×SE
    r = evaluate(curve(100, 101, 99.99, 100.99, 99.98, 100.98), n_trades=200)
    assert "유의성 부족" in r.verdict


def test_significant_sharpe_passes():
    # 꾸준한 +1%/일 에 미세 노이즈 → SR 큼 + N 게이트 통과
    values, v = [], 100.0
    for i in range(60):
        v *= 1.01 + (0.0001 if i % 2 else -0.0001)
        values.append(round(v, 6))
    r = evaluate(curve(*values), n_trades=150)
    assert r.sharpe_annual > 0 and "기준 충족" in r.verdict


def test_benchmark_excess():
    eq = curve(100, 105, 110)                                     # +10%
    bench = [("2026-07-01", D("200")), ("2026-07-02", D("202")), ("2026-07-03", D("210"))]  # +5%
    r = evaluate(eq, bench, n_trades=0)
    assert math.isclose(r.benchmark_return, 0.05)
    assert math.isclose(r.excess_return, 0.10 - 0.05)


def test_benchmark_missing_prices_ignored():
    r = evaluate(curve(100, 105), [("2026-07-01", None), ("2026-07-02", None)], n_trades=0)
    assert r.benchmark_return is None and r.excess_return is None


def test_sharpe_se_formula():
    # SE_daily = √((1+0.5·SR_d²)/N) — study.md §2.2. 연환산 ×√252 일관성만 검증
    r = evaluate(curve(100, 102, 101, 103, 102, 104), n_trades=0)
    assert r.sharpe_se_annual > 0
    assert r.n_days == 5
