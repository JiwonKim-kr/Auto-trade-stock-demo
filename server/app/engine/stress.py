"""합성 스트레스 샌드박스 — 토스 API 없이 안전장치 체인을 시나리오로 검증 (PLAN §7.1-B).

목적은 알파가 아니라 **증명**: "어떤 가격 경로에서도 서킷브레이커·손절·레짐·한도가 수식대로
동작해 극단 손실을 차단하는가". 합성 경로(랠리→폭락·갭·횡보)로 run_tick 전 체인을 구동한다.
판단기는 Deterministic(LLM 무관), 장부는 인메모리 페이퍼 — DB·네트워크 0.

스크리너/게이트는 완화 설정으로 '진입이 일어나게' 한 뒤(포지션이 있어야 손실 방어를 검증),
하락 구간에서 방어 장치의 발동 시점·손실 한정을 관측한다. 지그재그 랠리로 σ를 만들어
비용 게이트·레짐 필터도 활성 상태를 유지한다(체인 전체가 켜진 채 검증).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.engine.costs import EntryGate
from app.engine.exits import ExitConfig, evaluate_exits
from app.engine.paper import PaperPortfolio
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.regime import RegimeConfig
from app.engine.screener import ScreenConfig
from app.orders.guardrails import KST
from app.orders.models import OrderStatus, Side, TradingMode
from app.orders.service import OrderService

BENCH = "069500"
SIM_SCREEN = ScreenConfig(min_history=10, sma_short=3, sma_long=8, rsi_period=5,
                          rsi_overbought=101.0, min_avg_volume=1, min_close_price=1)
BASE_DAY = datetime(2026, 1, 5, 10, 0, tzinfo=KST)


def zigzag_rally(days: int, base: float = 10000.0) -> list[float]:
    """상승 + σ 확보용 지그재그(+4%/+1% 교대 → σ≈1.5%) — 게이트(문턱 1.68%)·레짐(ELEVATED)이
    켜진 채로 진입이 성립하게(σ 너무 낮으면 전 후보가 비용 게이트에 차단돼 체인 검증 불가)."""
    out, p = [], base
    for i in range(days):
        p *= 1.04 if i % 2 == 0 else 1.01
        out.append(p)
    return out


def extend(path: list[float], daily_returns: list[float]) -> list[float]:
    out, p = list(path), path[-1]
    for r in daily_returns:
        p *= 1 + r
        out.append(p)
    return out


@dataclass
class Scenario:
    name: str
    closes: list[float]                    # 시장 공통 경로(전 종목 + 벤치마크에 적용)
    warmup: int = 12                       # 스크리너 min_history 확보 후 거래 시작


SCENARIOS = [
    Scenario("crash_-7pct_x10", extend(zigzag_rally(20), [-0.07] * 10)),
    Scenario("gap_down_-25pct", extend(zigzag_rally(20), [-0.25, -0.03, -0.03])),
    Scenario("whipsaw_±3pct", extend(zigzag_rally(20), [0.03, -0.03] * 10)),
    Scenario("calm_rally", zigzag_rally(35)),
]


class SyntheticToss:
    """합성 시세 어댑터 — 시뮬레이션 커서(day)까지의 봉만 서빙(point-in-time)."""

    def __init__(self, symbols: list[str], closes: list[float]):
        self._paths = {s: [c * (1 + i * 0.001) for c in closes]
                       for i, s in enumerate([*symbols, BENCH])}
        self.day = 0

    async def get_stocks(self, symbols):
        from app.toss.models import Stock
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        from app.toss.models import Candle
        path = self._paths[symbol][: self.day + 1]
        return [Candle(timestamp=(BASE_DAY + timedelta(days=i)).isoformat(),
                       open_price=c, high_price=c, low_price=c, close_price=c,
                       volume=1_000_000, currency="KRW") for i, c in enumerate(path)]

    def mark(self, symbol: str) -> Decimal:
        return Decimal(str(self._paths[symbol][self.day]))


@dataclass
class StressResult:
    name: str
    final_equity: Decimal = Decimal(0)
    max_drawdown: float = 0.0
    cb_tripped_day: int | None = None
    forced_exits: int = 0
    buys_total: int = 0
    buys_after_trip: int = 0               # ★ 반드시 0 — 서킷브레이커 이후 신규 진입 금지 증명
    equity_curve: list[Decimal] = field(default_factory=list)

    def row(self) -> str:
        return (f"{self.name:>18} | 최종 {float(self.final_equity):>12,.0f} | "
                f"MDD {self.max_drawdown * 100:5.1f}% | CB일 {self.cb_tripped_day} | "
                f"손절 {self.forced_exits} | 매수 {self.buys_total} | CB후 매수 {self.buys_after_trip}")


async def simulate(scenario: Scenario, seed: Decimal = Decimal("1000000"),
                   minimal_defenses: bool = False) -> StressResult:
    """minimal_defenses=True: 게이트·레짐·손절을 끄고 서킷브레이커(최후 방어선)만 남긴다 —
    다층 방어에선 앞 층이 흡수해 CB 까지 도달하지 않으므로, CB 경로 자체는 이렇게 시험한다."""
    # 10종목 × 종목당 10% = 최대 노출 ~100% — 서킷브레이커(-15%)가 실제로 시험대에 오르게.
    # (기본 시드 1천만이면 한도가 노출을 ~5%로 묶어 CB 이전에 손절만으로 방어됨 — 그것도 유효한
    #  결론이지만, CB 발동·차단 경로 자체를 증명하려면 노출이 커야 한다.)
    symbols = [f"SIM{i:03d}0" for i in range(1, 11)]
    toss = SyntheticToss(symbols, scenario.closes)
    svc = OrderService(mode=TradingMode.DRY_RUN)
    svc.config = svc.config.model_copy(update={"enforce_market_hours": False})
    paper = PaperPortfolio(cash=seed)
    res = StressResult(name=scenario.name)
    opened_day: dict[str, int] = {}

    for day in range(scenario.warmup, len(scenario.closes)):
        toss.day = day
        now = BASE_DAY + timedelta(days=day)
        marks = {s: toss.mark(s) for s in [*symbols, BENCH]}
        days_held = {s: day - opened_day[s] for s in paper.positions if s in opened_day}
        exit_cfg = ExitConfig(enabled=not minimal_defenses)
        forced = evaluate_exits(paper.positions, marks, days_held, exit_cfg)
        tick = await run_tick(
            toss=toss, order_service=svc, watchlist=symbols, judge=DeterministicJudge(),
            now=now, screen_config=SIM_SCREEN,
            entry_gate=None if minimal_defenses else EntryGate(),
            regime_config=None if minimal_defenses else RegimeConfig(symbol=BENCH),
            holdings=paper.to_synthetic_holdings(marks), cash_buying_power_krw=paper.cash,
            forced_exits=forced,
        )
        res.forced_exits += len(forced)
        for o in tick.orders:
            if o.status is not OrderStatus.DRY_RUN:
                continue
            f = paper.apply_fill(o.request, EntryGate().cost, now=now)
            if f is None or f.skipped:
                continue
            if o.request.side is Side.BUY:
                res.buys_total += 1
                opened_day.setdefault(o.request.symbol, day)
                if res.cb_tripped_day is not None:
                    res.buys_after_trip += 1                        # 절대 일어나면 안 됨
            elif o.request.symbol not in paper.positions:
                opened_day.pop(o.request.symbol, None)
        equity, _ = paper.mark_equity(marks)
        res.equity_curve.append(equity)
        if res.cb_tripped_day is None and svc.circuit_breaker.tripped:
            res.cb_tripped_day = day

    res.final_equity = res.equity_curve[-1] if res.equity_curve else seed
    peak = Decimal(0)
    for e in res.equity_curve:
        peak = max(peak, e)
        if peak > 0:
            res.max_drawdown = max(res.max_drawdown, float((peak - e) / peak))
    return res
