"""틱 오케스트레이션 통합 테스트 (fake toss·판단기 — 네트워크/키 불필요).

매수 후보(000660, 미보유) + 보유(005930) → 조사(생략) → 판단 → 사이징 → DRY_RUN 주문.
최우선 검증: **실주문 0** (모두 DRY_RUN/REJECTED).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.engine.costs import CostConfig, EntryGate, EntryGateConfig
from app.engine.llm import Action, Decision
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.regime import RegimeConfig
from app.engine.screener import ScreenConfig
from app.orders.guardrails import KST
from app.orders.models import OrderStatus, Side, TradingMode
from app.orders.service import OrderService
from app.toss.models import BuyingPower, Candle, Holdings, Stock

OPEN_KST = datetime(2026, 6, 23, 10, 0, tzinfo=KST)   # 화요일 10:00 = 장중
LENIENT = ScreenConfig(min_history=5, sma_short=2, sma_long=5, rsi_period=3,
                       rsi_overbought=100.0, min_avg_volume=1, min_close_price=1)


def _rising_candles(base=1000, step=50, n=13) -> list[Candle]:
    return [Candle(timestamp=f"2026-06-{1 + i:02d}T00:00:00.000+09:00",
                   open_price=base + i * step, high_price=base + i * step,
                   low_price=base + i * step, close_price=base + i * step,
                   volume=1_000_000, currency="KRW") for i in range(n)]


def _holdings() -> Holdings:
    return Holdings.model_validate({
        "totalPurchaseAmount": {"krw": "229000"},
        "marketValue": {"amount": {"krw": "202500"}},
        "profitLoss": {"amount": {"krw": "-26500"}, "rate": "-0.1155"},
        "items": [{"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": "1",
                   "lastPrice": "202500", "averagePurchasePrice": "229000",
                   "marketValue": {"purchaseAmount": "229000", "amount": "202500"},
                   "profitLoss": {"amount": "-26500", "rate": "-0.1157"}}],
    })


class FakeToss:
    def __init__(self, candles):
        self._candles = candles

    async def get_holdings(self):
        return _holdings()

    async def get_stocks(self, symbols):
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        return self._candles

    async def get_buying_power(self, currency="KRW"):
        return BuyingPower(currency="KRW", cash_buying_power=Decimal("1000000"))


class BuyJudge:
    async def decide(self, ctx):
        if ctx.symbol == "000660":
            return Decision(action=Action.BUY, symbol="000660", confidence=0.8, rationale="t")
        return Decision(action=Action.HOLD, symbol=ctx.symbol, confidence=0.3, rationale="t")


async def test_tick_buy_candidate_produces_dry_run_order():
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT)
    # 후보: 000660(매수) + 005930(보유) 모두 평가
    assert res.candidates == 2
    actions = {d.symbol: d.action for d in res.decisions}
    assert actions["000660"] is Action.BUY and actions["005930"] is Action.HOLD

    # 주문: 000660 BUY 1건, DRY_RUN(미전송)
    assert len(res.orders) == 1
    o = res.orders[0]
    assert o.request.symbol == "000660" and o.request.side is Side.BUY
    assert o.status is OrderStatus.DRY_RUN and o.sent_to_market is False
    assert o.request.quantity >= 1

    # ★ 실주문 0
    assert all(x.status is not OrderStatus.SUBMITTED for x in res.orders)


async def test_tick_deterministic_fallback_runs():
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=DeterministicJudge(), now=OPEN_KST,
                         screen_config=LENIENT)
    assert res.candidates == 2
    assert all(x.status is not OrderStatus.SUBMITTED for x in res.orders)   # 실주문 0


async def test_tick_kill_switch_rejects_orders():
    svc = OrderService(mode=TradingMode.DRY_RUN)
    svc.engage_kill_switch()
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT)
    assert res.orders and all(x.status is OrderStatus.REJECTED for x in res.orders)


async def test_tick_circuit_breaker_halts_new_buys():
    # 일일 손실 -6% → 서킷브레이커 발동 → 신규 매수(000660) 차단
    class LossToss(FakeToss):
        async def get_holdings(self):
            return Holdings.model_validate({
                "totalPurchaseAmount": {"krw": "229000"},
                "marketValue": {"amount": {"krw": "202500"}},
                "profitLoss": {"amount": {"krw": "-26500"}, "rate": "-0.1155"},
                "dailyProfitLoss": {"amount": {"krw": "-70000"}, "rate": "-0.06"},
                "items": [{"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": "1",
                           "lastPrice": "202500", "averagePurchasePrice": "229000",
                           "marketValue": {"purchaseAmount": "229000", "amount": "202500"},
                           "profitLoss": {"amount": "-26500", "rate": "-0.1157"}}],
            })

    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=LossToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT)
    assert res.circuit_breaker is True and "일일 손실" in res.circuit_breaker_reason
    buys = [o for o in res.orders if o.request.side is Side.BUY]
    assert buys and all(
        o.status is OrderStatus.REJECTED and "CIRCUIT_BREAKER" in o.reason for o in buys
    )


async def test_tick_cost_gate_blocks_low_edge_buy():
    # 완만한 상승(저변동성) → 기대이동폭 < 비용 문턱 → 매수 차단
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, entry_gate=EntryGate())
    assert "000660" in res.cost_gated
    assert not [o for o in res.orders if o.request.side is Side.BUY]   # 매수 주문 없음


async def test_tick_cost_gate_allows_when_disabled():
    # cost_multiple=0 → 문턱 0 → 게이트 통과 → 매수 주문 생성(게이트 배선 반대 방향 검증)
    svc = OrderService(mode=TradingMode.DRY_RUN)
    gate = EntryGate(CostConfig(), EntryGateConfig(cost_multiple=Decimal("0")))
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, entry_gate=gate)
    assert res.cost_gated == []
    assert [o for o in res.orders if o.request.side is Side.BUY]       # 매수 주문 존재


def _volatile_candles(swing=0.03, n=13) -> list[Candle]:
    closes, price = [], 1000.0
    for i in range(n):
        closes.append(price)
        price *= (1 + swing) if i % 2 == 0 else (1 - swing)
    return [Candle(timestamp=f"2026-06-{1 + i:02d}T00:00:00.000+09:00",
                   open_price=c, high_price=c, low_price=c, close_price=c,
                   volume=1_000_000, currency="KRW") for i, c in enumerate(closes)]


class RegimeToss(FakeToss):
    """레짐 프록시(069500)만 고변동 캔들 — 후보 종목은 평온한 상승 유지."""

    async def get_candles(self, symbol, interval="1d"):
        if symbol == "069500":
            return _volatile_candles()
        return await super().get_candles(symbol, interval)


async def test_tick_stress_regime_blocks_new_buys():
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=RegimeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, regime_config=RegimeConfig())
    assert res.regime["level"] == "STRESS" and res.regime["multiplier"] == "0"
    assert not [o for o in res.orders if o.request.side is Side.BUY]   # 신규 매수 없음


async def test_tick_calm_regime_normal_sizing():
    # 프록시도 완만한 상승(σ 작음) → CALM ×1 → 매수 정상 생성
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, regime_config=RegimeConfig())
    assert res.regime["level"] == "CALM" and res.regime["multiplier"] == "1"
    assert [o for o in res.orders if o.request.side is Side.BUY]


async def test_tick_regime_fetch_failure_is_unknown_multiplier_one():
    class FailToss(FakeToss):
        async def get_candles(self, symbol, interval="1d"):
            if symbol == "069500":
                raise RuntimeError("프록시 조회 실패")
            return await super().get_candles(symbol, interval)

    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FailToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, regime_config=RegimeConfig())
    assert res.regime["level"] == "UNKNOWN" and res.regime["multiplier"] == "1"
    assert [o for o in res.orders if o.request.side is Side.BUY]       # 매수 막히지 않음


async def test_tick_daily_buy_used_carries_across_ticks():
    # 오늘 이미 490,000 사용(DB 주입 가정) + cap 500,000 → 이번 틱 매수(≈16,000)도 일일 한도 초과
    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=FakeToss(_rising_candles()), order_service=svc,
                         watchlist=["000660"], judge=BuyJudge(), now=OPEN_KST,
                         screen_config=LENIENT, daily_buy_used_krw=Decimal("490000"))
    buys = [o for o in res.orders if o.request.side is Side.BUY]
    assert buys and all(
        o.status is OrderStatus.REJECTED and "DAILY_BUY_CAP" in o.reason for o in buys
    )


async def test_tick_max_buy_candidates_compresses_by_score():
    # 매수 후보 3 종목 → score 상위 2만 판단(LLM 비용 가드). 보유(005930)는 항상 평가.
    class MultiToss(FakeToss):
        async def get_candles(self, symbol, interval="1d"):
            # 종목별로 기울기 다른 상승 → score 차등(가파를수록 높음)
            step = {"000660": 10, "035420": 30, "035720": 50}.get(symbol, 5)
            return _rising_candles(step=step)

    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=MultiToss(_rising_candles()), order_service=svc,
                         watchlist=["000660", "035420", "035720"], judge=BuyJudge(),
                         now=OPEN_KST, screen_config=LENIENT, max_buy_candidates=2)
    symbols = {d.symbol for d in res.decisions}
    assert res.candidates == 3                                # 상위 2 매수후보 + 보유 1
    assert "000660" not in symbols                            # 최저 score 탈락
    assert {"035420", "035720", "005930"} <= symbols


async def test_tick_no_symbols_noop():
    class EmptyToss(FakeToss):
        async def get_holdings(self):
            return Holdings.model_validate({
                "totalPurchaseAmount": {"krw": "0"},
                "marketValue": {"amount": {"krw": "0"}},
                "profitLoss": {"amount": {"krw": "0"}, "rate": "0"},
                "items": [],
            })

    svc = OrderService(mode=TradingMode.DRY_RUN)
    res = await run_tick(toss=EmptyToss(_rising_candles()), order_service=svc,
                         watchlist=[], judge=BuyJudge(), now=OPEN_KST)
    assert res.candidates == 0 and res.orders == [] and "심볼 없음" in res.note
