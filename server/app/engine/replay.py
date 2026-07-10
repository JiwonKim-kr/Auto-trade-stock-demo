"""히스토리컬 리플레이(백테스트) — 토스 API 없이 과거 일봉으로 결정적 전략 검증 (PLAN §7.1-A).

규율(study.md §7 계승):
  - **point-in-time**: 시뮬레이션 날짜 T 이후의 봉은 절대 노출하지 않는다(ReplayToss 가 강제).
  - **다음 시가 체결**: T 종가 신호의 주문은 T+1 시가로 체결(같은 봉 종가 진입 = 미래정보 누출).
    갭업으로 현금 초과 시 미체결 소멸(보수), 다음 봉이 없으면(기간 끝/상폐) 소멸.
  - 비용은 CostConfig(진입 게이트·페이퍼와 동일 모델), 거래일 수 = 데이터 날짜 인덱스 차(정확).

한계(정직하게 — 결과 해석 시 필수):
  - **LLM 판단은 소급 평가 불가**(훈련데이터 look-ahead 오염) — 판단기는 Deterministic 전용.
    이 리플레이의 유효 범위 = 결정적 구성요소 검증 + 파라미터 상대 비교.
  - **생존편향** — 현재 상장 종목만 담긴 데이터면 절대 성과는 상향 편향.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.engine.costs import CostConfig, EntryGate
from app.engine.evaluation import EvalReport, evaluate
from app.engine.exits import ExitConfig, evaluate_exits
from app.engine.paper import PaperPortfolio
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.regime import RegimeConfig
from app.engine.screener import ScreenConfig
from app.orders.guardrails import KST
from app.orders.models import OrderRequest, OrderStatus, Side, TradingMode
from app.orders.service import OrderService

Bar = dict  # {"date": "YYYY-MM-DD", "open","high","low","close","volume": float}


class ReplayToss:
    """과거 일봉 어댑터 — current_date 까지의 봉만 서빙(point-in-time 강제)."""

    def __init__(self, histories: dict[str, list[Bar]]):
        self._h = {s: sorted(bars, key=lambda b: b["date"]) for s, bars in histories.items()}
        self.current_date = ""

    async def get_stocks(self, symbols):
        from app.toss.models import Stock
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        from app.toss.models import Candle
        return [Candle(timestamp=f"{b['date']}T00:00:00+09:00", open_price=b["open"],
                       high_price=b["high"], low_price=b["low"], close_price=b["close"],
                       volume=b["volume"], currency="KRW")
                for b in self._h.get(symbol, []) if b["date"] <= self.current_date]

    def close_on(self, symbol: str, date: str) -> Decimal | None:
        for b in self._h.get(symbol, []):
            if b["date"] == date:
                return Decimal(str(b["close"]))
        return None

    def open_on(self, symbol: str, date: str) -> Decimal | None:
        for b in self._h.get(symbol, []):
            if b["date"] == date:
                return Decimal(str(b["open"]))
        return None


@dataclass
class BacktestResult:
    equity_curve: list[tuple[str, Decimal, Decimal | None]] = field(default_factory=list)
    eval_report: EvalReport | None = None
    trade_count: int = 0
    buys: int = 0
    unfilled: int = 0                     # 다음 시가 체결 실패(소멸) 수 — 보수 규율의 흔적
    paper: PaperPortfolio | None = None   # 최종 장부(검증·분석용)


async def run_backtest(
    histories: dict[str, list[Bar]],
    *,
    benchmark: str | None = None,
    seed: Decimal = Decimal("10000000"),
    screen_config: ScreenConfig | None = None,
    exit_config: ExitConfig | None = None,
    warmup: int = 25,
) -> BacktestResult:
    symbols = [s for s in histories if s != benchmark]
    dates = sorted({b["date"] for bars in histories.values() for b in bars})
    date_idx = {d: i for i, d in enumerate(dates)}
    toss = ReplayToss(histories)
    svc = OrderService(mode=TradingMode.DRY_RUN)
    svc.config = svc.config.model_copy(update={"enforce_market_hours": False})
    paper = PaperPortfolio(cash=seed)
    cost = CostConfig()
    res = BacktestResult()
    pending: list[OrderRequest] = []
    opened: dict[str, str] = {}

    for d in dates[warmup:]:
        toss.current_date = d
        now = datetime.fromisoformat(f"{d}T15:30:00").replace(tzinfo=KST)

        # 1) 전일 신호 주문을 오늘 시가로 체결(다음 시가 규율)
        for req in pending:
            open_px = toss.open_on(req.symbol, d)
            if open_px is None:
                res.unfilled += 1
                continue
            f = paper.apply_fill(req.model_copy(update={"price": open_px}), cost, now=now)
            if f is None or f.skipped:
                res.unfilled += 1
                continue
            if req.side is Side.BUY:
                res.buys += 1
                opened.setdefault(req.symbol, d)
            elif req.symbol not in paper.positions:
                opened.pop(req.symbol, None)
        pending = []

        # 2) 오늘 종가 기준 신호 생성(체결은 내일)
        marks = {s: px for s in [*symbols, *( [benchmark] if benchmark else [] )]
                 if (px := toss.close_on(s, d)) is not None}
        days_held = {s: date_idx[d] - date_idx[opened[s]]
                     for s in paper.positions if s in opened and opened[s] in date_idx}
        forced = evaluate_exits(paper.positions, marks, days_held,
                                exit_config or ExitConfig())
        tick = await run_tick(
            toss=toss, order_service=svc, watchlist=symbols, judge=DeterministicJudge(),
            now=now, screen_config=screen_config, entry_gate=EntryGate(),
            regime_config=RegimeConfig(symbol=benchmark) if benchmark else None,
            holdings=paper.to_synthetic_holdings(marks), cash_buying_power_krw=paper.cash,
            forced_exits=forced,
        )
        pending = [o.request for o in tick.orders if o.status is OrderStatus.DRY_RUN]

        equity, _ = paper.mark_equity(marks)
        res.equity_curve.append((d, equity, marks.get(benchmark) if benchmark else None))

    res.trade_count = paper.trade_count
    res.paper = paper
    res.eval_report = evaluate([(d, e) for d, e, _ in res.equity_curve],
                               [(d, b) for d, _, b in res.equity_curve],
                               n_trades=paper.trade_count)
    return res
