"""비용 모델 + 진입 게이트 테스트."""

from __future__ import annotations

from decimal import Decimal

from app.engine.costs import (
    CostConfig,
    EntryGate,
    EntryGateConfig,
    realized_daily_vol,
)


# ── 비용 모델 ─────────────────────────────────────────────────────────────────
def test_round_trip_rate_default():
    # 2×0.00015(수수료) + 2×0.0015(슬리피지) + 0.0015(매도세) = 0.0048
    assert CostConfig().round_trip_rate() == Decimal("0.0048")


def test_round_trip_rate_configurable():
    c = CostConfig(commission_rate=Decimal("0"), slippage_rate=Decimal("0"),
                   sell_tax_rate=Decimal("0.002"))
    assert c.round_trip_rate() == Decimal("0.002")


# ── 실현 변동성 ───────────────────────────────────────────────────────────────
def test_realized_vol_none_when_insufficient():
    assert realized_daily_vol(None) is None
    assert realized_daily_vol([100.0]) is None
    assert realized_daily_vol([100.0, 101.0], min_returns=5) is None   # 1 수익률 < 5


def test_realized_vol_flat_series_is_zero():
    assert realized_daily_vol([100.0] * 8, min_returns=3) == Decimal("0")


def test_realized_vol_positive_for_noisy_series():
    vol = realized_daily_vol([100, 105, 99, 108, 96, 110, 94], min_returns=3)
    assert vol is not None and vol > Decimal("0.05")


# ── 진입 게이트 ───────────────────────────────────────────────────────────────
def test_gate_passes_high_edge():
    gate = EntryGate()
    closes = [100, 108, 96, 110, 94, 112, 92, 114, 90, 116]   # σ 매우 큼
    r = gate.evaluate(confidence=0.9, closes=closes)
    assert r.passed and r.expected_move_rate >= r.hurdle_rate


def test_gate_blocks_low_edge():
    gate = EntryGate()
    # 저변동성(σ 작음) + 낮은 confidence → 기대이동 < 문턱(1.68%)
    closes = [100.0, 100.2, 100.1, 100.3, 100.2, 100.4, 100.3, 100.5]
    r = gate.evaluate(confidence=0.3, closes=closes)
    assert not r.passed and "<" in r.reason


def test_gate_blocks_when_vol_unknown():
    r = EntryGate().evaluate(confidence=1.0, closes=[100.0, 101.0])   # 표본 부족
    assert not r.passed and "표본 부족" in r.reason


def test_gate_hurdle_uses_cost_multiple():
    gate = EntryGate(CostConfig(), EntryGateConfig(cost_multiple=Decimal("3.5")))
    assert gate.hurdle_rate() == Decimal("0.0048") * Decimal("3.5")   # ≈ 1.68%


def test_gate_disabled_when_multiple_zero():
    gate = EntryGate(CostConfig(), EntryGateConfig(cost_multiple=Decimal("0")))
    r = gate.evaluate(confidence=0.01, closes=[100.0, 100.1, 100.0, 100.1, 100.0, 100.1])
    assert r.passed and r.hurdle_rate == Decimal("0")   # 문턱 0 → 항상 통과
