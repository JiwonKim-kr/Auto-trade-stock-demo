"""스크리너 테스트."""

from __future__ import annotations

import json
from pathlib import Path

from app.engine.screener import ScreenConfig, screen_candidates, screen_symbol
from app.toss.models import Candle, CandleSeries

FIX = Path(__file__).parent / "fixtures"


def candle(close, vol=1_000_000, ts="2026-01-01T00:00:00.000+09:00") -> Candle:
    return Candle(timestamp=ts, open_price=close, high_price=close, low_price=close,
                  close_price=close, volume=vol, currency="KRW")


def series(closes, vol=1_000_000) -> list[Candle]:
    return [candle(c, vol, ts=f"2026-01-{i + 1:02d}T00:00:00.000+09:00")
            for i, c in enumerate(closes)]   # 과거→최신


def test_uptrend_passes():
    cfg = ScreenConfig(min_history=5, sma_short=2, sma_long=5, rsi_period=3,
                       rsi_overbought=100.0, min_avg_volume=1000, min_close_price=1000)
    r = screen_symbol("UP", series([1000, 1100, 1200, 1300, 1400, 1500]), cfg)
    assert r.passed and r.score > 0 and r.reasons == []


def test_penny_stock_excluded():
    cfg = ScreenConfig(min_history=3, sma_long=2, min_close_price=1000,
                       min_avg_volume=1, require_uptrend=False)
    r = screen_symbol("PENNY", series([500, 510, 520]), cfg)
    assert not r.passed and any("penny_stock" in x for x in r.reasons)


def test_illiquid_excluded():
    cfg = ScreenConfig(min_history=3, sma_long=2, min_avg_volume=1_000_000,
                       min_close_price=1, require_uptrend=False)
    r = screen_symbol("ILQ", series([2000, 2100, 2200], vol=100), cfg)
    assert not r.passed and any("illiquid" in x for x in r.reasons)


def test_insufficient_history():
    r = screen_symbol("SHORT", series([1000, 1100, 1200]), ScreenConfig(min_history=20))
    assert not r.passed and any("insufficient_history" in x for x in r.reasons)


def test_overbought_excluded():
    cfg = ScreenConfig(min_history=5, sma_short=2, sma_long=5, rsi_period=3,
                       rsi_overbought=70.0, min_avg_volume=1000, min_close_price=1000)
    r = screen_symbol("HOT", series([1000, 1100, 1200, 1300, 1400, 1500]), cfg)
    assert not r.passed and any("overbought" in x for x in r.reasons)


def test_downtrend_fails_uptrend():
    cfg = ScreenConfig(min_history=5, sma_short=2, sma_long=5, rsi_overbought=100.0,
                       min_avg_volume=1000, min_close_price=1000, require_uptrend=True)
    r = screen_symbol("DOWN", series([1500, 1400, 1300, 1200, 1100, 1000]), cfg)
    assert not r.passed and any("below_sma_long" in x for x in r.reasons)


def test_screen_candidates_ranks_passed_only():
    cfg = ScreenConfig(min_history=5, sma_short=2, sma_long=5, rsi_period=3,
                       rsi_overbought=100.0, min_avg_volume=1000, min_close_price=1000)
    data = {
        "STRONG": series([1000, 1100, 1200, 1300, 1400, 1600]),
        "MILD": series([1000, 1010, 1020, 1030, 1040, 1050]),
        "DOWN": series([1500, 1400, 1300, 1200, 1100, 1000]),
    }
    out = screen_candidates(data, cfg)
    syms = [r.symbol for r in out]
    assert "DOWN" not in syms
    assert set(syms) == {"STRONG", "MILD"}
    assert syms[0] == "STRONG"     # 더 강한 모멘텀이 먼저


def test_real_fixture_runs():
    raw = json.loads((FIX / "candles.json").read_text(encoding="utf-8"))["result"]
    candles = CandleSeries.model_validate(raw).candles
    candles.sort(key=lambda c: c.timestamp)
    cfg = ScreenConfig(min_history=10, sma_short=5, sma_long=10, rsi_period=10,
                       min_avg_volume=1, min_close_price=1, require_uptrend=False)
    r = screen_symbol("005930", candles, cfg)
    assert r.indicators.last_close == 310500.0     # 06-23 최신 close
    assert r.indicators.sma_long is not None
    assert r.indicators.avg_volume > 0
