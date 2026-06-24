"""기술지표 — 순수 함수(의존성 없음). 입력은 **과거→최신** 순 종가 리스트(float).

지표 계산은 휴리스틱 신호이므로 float 로 충분(돈 계산 아님 → Decimal 불필요).
지표가 많아지면 pandas-ta 로 교체 가능. 지금은 SMA/RSI 만 하드코딩(테스트 용이).
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    """단순이동평균. 데이터가 period 보다 적으면 None."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder RSI. 데이터가 period+1 보다 적으면 None. 전부 상승=100, 전부 하락=0."""
    if period <= 0 or len(values) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):           # Wilder 스무딩
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
