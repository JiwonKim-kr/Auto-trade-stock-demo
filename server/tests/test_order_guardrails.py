"""주문층/가드레일 안전 테스트.

최우선 증명: **DRY_RUN 에서 실주문이 0** (executor 미호출).
그 외: 킬스위치·1주문한도·일일한도·최대포지션·장시간·비중·멱등·LIVE 경로·모드 다중확인.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.config import load_trading_mode
from app.orders.guardrails import KST, GuardrailConfig, GuardrailContext
from app.orders.models import OrderRequest, OrderStatus, OrderType, Side, TradingMode
from app.orders.service import CallableExecutor, OrderService

OPEN_KST = datetime(2026, 6, 23, 10, 0, tzinfo=KST)   # 화요일 10:00 KST = 장중


def buy(symbol="005930", qty="1", price="50000", **kw) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side=Side.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal(qty), price=Decimal(price), **kw,
    )


def ctx(**kw) -> GuardrailContext:
    base = dict(now=OPEN_KST)
    base.update(kw)
    return GuardrailContext(**base)


# ── 최우선: DRY_RUN 실주문 0 ──────────────────────────────────────────────────
def test_dry_run_never_calls_executor():
    def explode(_order):
        raise AssertionError("DRY_RUN must NOT place real orders")

    svc = OrderService(mode=TradingMode.DRY_RUN, executor=CallableExecutor(explode))
    res = svc.submit(buy(), ctx())
    assert res.status is OrderStatus.DRY_RUN
    assert res.sent_to_market is False
    assert svc.sent_orders == []
    assert len(svc.intended_orders) == 1


# ── 가드레일 ──────────────────────────────────────────────────────────────────
def test_kill_switch_blocks():
    svc = OrderService()
    svc.engage_kill_switch()
    res = svc.submit(buy(), ctx())
    assert res.status is OrderStatus.REJECTED and "KILL_SWITCH" in res.reason


def test_per_order_max_blocks_oversized_buy():
    svc = OrderService(config=GuardrailConfig(per_order_max_krw=Decimal("100000")))
    res = svc.submit(buy(qty="3", price="50000"), ctx())   # 150,000 > 100,000
    assert res.status is OrderStatus.REJECTED and "PER_ORDER_MAX" in res.reason


def test_unbounded_market_buy_blocked():
    svc = OrderService()
    o = OrderRequest(symbol="005930", side=Side.BUY, order_type=OrderType.MARKET, quantity=Decimal("1"))
    res = svc.submit(o, ctx())
    assert res.status is OrderStatus.REJECTED and "UNBOUNDED_COST" in res.reason


def test_daily_buy_cap_blocks():
    svc = OrderService(config=GuardrailConfig(daily_buy_cap_krw=Decimal("100000")))
    res = svc.submit(buy(qty="1", price="50000"), ctx(daily_buy_used_krw=Decimal("80000")))
    assert res.status is OrderStatus.REJECTED and "DAILY_BUY_CAP" in res.reason


def test_max_positions_blocks_new_symbol():
    svc = OrderService(config=GuardrailConfig(max_positions=2))
    res = svc.submit(buy(symbol="000660"),
                     ctx(open_positions=2, held_symbols=frozenset({"005930", "005380"})))
    assert res.status is OrderStatus.REJECTED and "MAX_POSITIONS" in res.reason


def test_add_to_held_symbol_not_blocked_by_positions():
    svc = OrderService(config=GuardrailConfig(max_positions=2))
    res = svc.submit(buy(symbol="005930"),
                     ctx(open_positions=2, held_symbols=frozenset({"005930", "005380"})))
    assert res.status is OrderStatus.DRY_RUN   # 기존 종목 추가매수 → 포지션 수 불변


def test_market_hours_blocks_after_close():
    after = datetime(2026, 6, 23, 18, 0, tzinfo=KST)   # 15:30 이후
    svc = OrderService()
    res = svc.submit(buy(), ctx(now=after))
    assert res.status is OrderStatus.REJECTED and "MARKET_CLOSED" in res.reason


def test_market_hours_blocks_weekend():
    sat = datetime(2026, 6, 27, 10, 0, tzinfo=KST)     # 토요일
    svc = OrderService()
    res = svc.submit(buy(), ctx(now=sat))
    assert res.status is OrderStatus.REJECTED and "MARKET_CLOSED" in res.reason


def test_per_symbol_weight_blocks():
    svc = OrderService(config=GuardrailConfig(
        per_symbol_max_weight=Decimal("0.30"),
        per_order_max_krw=Decimal("1000000"),
        daily_buy_cap_krw=Decimal("10000000"),
    ))
    # 비중 = 100000 / (200000+100000) = 33.3% > 30%
    res = svc.submit(buy(symbol="005930", qty="1", price="100000"),
                     ctx(portfolio_value_krw=Decimal("200000"),
                         symbol_current_value_krw=Decimal("0")))
    assert res.status is OrderStatus.REJECTED and "PER_SYMBOL_WEIGHT" in res.reason


def test_sell_skips_buy_guardrails():
    svc = OrderService(config=GuardrailConfig(per_order_max_krw=Decimal("1")))
    sell = OrderRequest(symbol="005930", side=Side.SELL, order_type=OrderType.MARKET,
                        quantity=Decimal("1"))
    res = svc.submit(sell, ctx())
    assert res.status is OrderStatus.DRY_RUN   # 매도는 매수 한도 무관


# ── 멱등 ──────────────────────────────────────────────────────────────────────
def test_idempotency_returns_prior_without_resubmitting():
    calls: list[str] = []

    def place(order):
        calls.append(order.client_order_id)
        return "toss-123"

    svc = OrderService(mode=TradingMode.LIVE, executor=CallableExecutor(place))
    r1 = svc.submit(buy(client_order_id="fixed-1"), ctx())
    r2 = svc.submit(buy(client_order_id="fixed-1"), ctx())
    assert r1.status is OrderStatus.SUBMITTED and r1.toss_order_id == "toss-123"
    assert r2.status is OrderStatus.DUPLICATE
    assert calls == ["fixed-1"]   # executor 단 1회


# ── LIVE 경로 ─────────────────────────────────────────────────────────────────
def test_live_submits_when_guardrails_pass():
    svc = OrderService(mode=TradingMode.LIVE, executor=CallableExecutor(lambda _o: "toss-xyz"))
    res = svc.submit(buy(), ctx())
    assert res.status is OrderStatus.SUBMITTED and res.toss_order_id == "toss-xyz"
    assert res.sent_to_market is True


def test_live_without_executor_refuses():
    svc = OrderService(mode=TradingMode.LIVE, executor=None)
    res = svc.submit(buy(), ctx())
    assert res.status is OrderStatus.FAILED and "executor" in res.reason.lower()


def test_kill_switch_blocks_live_before_executor():
    def explode(_order):
        raise AssertionError("must not reach executor")

    svc = OrderService(mode=TradingMode.LIVE, executor=CallableExecutor(explode))
    svc.engage_kill_switch()
    res = svc.submit(buy(), ctx())
    assert res.status is OrderStatus.REJECTED   # 가드레일이 모드보다 먼저


# ── 모드 다중 확인 ────────────────────────────────────────────────────────────
def test_trading_mode_defaults_dry_run():
    mode, warns = load_trading_mode(env={})
    assert mode is TradingMode.DRY_RUN and warns == []


def test_trading_mode_live_requires_confirmation():
    mode, warns = load_trading_mode(env={"TRADING_MODE": "LIVE"})
    assert mode is TradingMode.DRY_RUN and warns   # 확인 없으면 강등 + 경고


def test_trading_mode_live_with_confirmation():
    mode, warns = load_trading_mode(
        env={"TRADING_MODE": "LIVE", "I_UNDERSTAND_LIVE_REAL_MONEY": "YES"}
    )
    assert mode is TradingMode.LIVE and warns
