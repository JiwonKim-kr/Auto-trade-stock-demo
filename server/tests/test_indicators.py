"""기술지표 단위 테스트 (손계산 가능한 앵커 값)."""

from __future__ import annotations

from app.engine.indicators import rsi, sma


def test_sma():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2, 3, 4, 5], 2) == 4.5
    assert sma([1, 2], 5) is None
    assert sma([1, 2, 3], 0) is None


def test_rsi_monotonic_bounds():
    assert rsi(list(range(1, 17)), 14) == 100.0       # 전부 상승
    assert rsi(list(range(16, 0, -1)), 14) == 0.0      # 전부 하락


def test_rsi_known_value():
    # [10,11,10,11], period 2 → 손계산 RSI = 75.0
    assert rsi([10, 11, 10, 11], 2) == 75.0


def test_rsi_too_few():
    assert rsi([1, 2, 3], 14) is None
