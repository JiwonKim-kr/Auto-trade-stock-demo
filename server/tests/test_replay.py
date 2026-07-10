"""리플레이 백테스트 테스트 — point-in-time·다음 시가 체결 규율 + 미니 구동."""

from __future__ import annotations

from app.engine.replay import ReplayToss, run_backtest
from app.engine.stress import SIM_SCREEN


def _bars(days: int, base: float = 10000.0, open_offset: float = 0.0) -> list[dict]:
    """지그재그 상승 일봉. open_offset>0 이면 시가를 종가와 뚜렷이 분리(체결 규율 검증용)."""
    out, p = [], base
    for i in range(days):
        p *= 1.04 if i % 2 == 0 else 1.01
        out.append({"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}",
                    "open": p + open_offset, "high": p * 1.01, "low": p * 0.99,
                    "close": p, "volume": 1_000_000})
    return out


async def test_point_in_time_slicing():
    toss = ReplayToss({"A00010": _bars(30)})
    toss.current_date = _bars(30)[5]["date"]
    candles = await toss.get_candles("A00010")
    assert len(candles) == 6                                     # 미래 봉 미노출
    assert max(c.timestamp.strftime("%Y-%m-%d") for c in candles) == toss.current_date


async def test_next_open_fill_discipline():
    # 시가를 종가보다 +20,000 위로 — 체결가가 시가 기반이면 avg_cost 가 종가대(≈1만~1.5만)를
    # 크게 상회해야 한다(같은 봉 종가 체결이면 이 테스트가 잡아낸다)
    hist = {"A00010": _bars(30, open_offset=20000.0)}
    r = await run_backtest(hist, benchmark=None, screen_config=SIM_SCREEN, warmup=12)
    assert r.buys > 0 and r.paper is not None
    pos = next(iter(r.paper.positions.values()), None)
    if pos is None:                                              # 전량 청산됐어도 매수는 있었음
        assert r.trade_count > 0
    else:
        assert pos.avg_cost > 20000                              # 시가(D+1) 기반 체결 증명


async def test_mini_backtest_runs_and_evaluates():
    hist = {"A00010": _bars(40), "B00020": _bars(40, base=12000.0), "069500": _bars(40)}
    r = await run_backtest(hist, benchmark="069500", screen_config=SIM_SCREEN, warmup=12)
    assert len(r.equity_curve) == 40 - 12
    assert r.buys > 0
    assert r.eval_report is not None and r.eval_report.n_days > 0
    assert r.eval_report.benchmark_return is not None            # 벤치마크 동시 기록