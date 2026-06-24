"""결정적 기술지표 스크리너 — 유니버스 적격 종목 → 소수 후보로 압축 (인사이트 §5 엔진 1단계).

입력: 종목별 캔들(과거→최신). 출력: 통과 후보 + 지표 + 사유, score 로 랭킹.
여기서 거른 결과가 LLM(2단계)에 넘어간다. 순수·결정적이라 LLM 환각이 못 넘는 사전 필터다.

필터(기본): 충분한 히스토리 · 동전주 제외 · 저유동성 제외 · 상승추세(종가>장기SMA) · 과매수 제외.
한도는 ScreenConfig 로 조정. (저유동성/동전주는 후보 단계 per-symbol — 인사이트 §5)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.indicators import rsi, sma
from app.toss.models import Candle


@dataclass(frozen=True)
class ScreenConfig:
    min_history: int = 20
    sma_short: int = 5
    sma_long: int = 20
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    min_avg_volume: float = 100_000.0     # 평균 거래량(유동성)
    min_close_price: float = 1_000.0      # 동전주 제외(원)
    require_uptrend: bool = True


@dataclass
class ScreenIndicators:
    last_close: float
    sma_short: float | None
    sma_long: float | None
    rsi: float | None
    avg_volume: float


@dataclass
class ScreenResult:
    symbol: str
    passed: bool
    score: float
    indicators: ScreenIndicators
    reasons: list[str] = field(default_factory=list)   # 탈락/주의 사유


def _score(ind: ScreenIndicators) -> float:
    """모멘텀 강도(랭킹용): 단기SMA가 장기SMA 위로 얼마나 떨어져 있는가."""
    if ind.sma_short is not None and ind.sma_long:
        return (ind.sma_short - ind.sma_long) / ind.sma_long
    return 0.0


def screen_symbol(symbol: str, candles: list[Candle], cfg: ScreenConfig | None = None) -> ScreenResult:
    cfg = cfg or ScreenConfig()
    closes = [float(c.close_price) for c in candles]   # 과거→최신 가정
    volumes = [float(c.volume) for c in candles]

    ind = ScreenIndicators(
        last_close=closes[-1] if closes else 0.0,
        sma_short=sma(closes, cfg.sma_short),
        sma_long=sma(closes, cfg.sma_long),
        rsi=rsi(closes, cfg.rsi_period),
        avg_volume=(sum(volumes) / len(volumes)) if volumes else 0.0,
    )

    reasons: list[str] = []
    if len(closes) < cfg.min_history:
        reasons.append(f"insufficient_history({len(closes)}<{cfg.min_history})")
    if ind.last_close < cfg.min_close_price:
        reasons.append(f"penny_stock(close={ind.last_close:.0f}<{cfg.min_close_price:.0f})")
    if ind.avg_volume < cfg.min_avg_volume:
        reasons.append(f"illiquid(avg_vol={ind.avg_volume:.0f}<{cfg.min_avg_volume:.0f})")
    if cfg.require_uptrend and ind.sma_long is not None and ind.last_close < ind.sma_long:
        reasons.append("below_sma_long(no_uptrend)")
    if ind.rsi is not None and ind.rsi > cfg.rsi_overbought:
        reasons.append(f"overbought(rsi={ind.rsi:.1f}>{cfg.rsi_overbought:.0f})")

    return ScreenResult(symbol=symbol, passed=(len(reasons) == 0), score=_score(ind),
                        indicators=ind, reasons=reasons)


def screen_candidates(
    candles_by_symbol: dict[str, list[Candle]],
    cfg: ScreenConfig | None = None,
    top_n: int | None = None,
) -> list[ScreenResult]:
    """여러 종목 스크리닝 → 통과분만 score 내림차순. top_n 이면 상위 N."""
    cfg = cfg or ScreenConfig()
    passed = [r for sym, cs in candles_by_symbol.items()
              if (r := screen_symbol(sym, cs, cfg)).passed]
    passed.sort(key=lambda r: r.score, reverse=True)
    return passed[:top_n] if top_n is not None else passed
