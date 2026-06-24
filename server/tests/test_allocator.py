"""결정적 사이징 allocator 테스트."""

from __future__ import annotations

from decimal import Decimal

from app.engine.allocator import allocate
from app.engine.llm import Action, CandidateContext, Decision
from app.engine.screener import ScreenIndicators
from app.orders.guardrails import GuardrailConfig
from app.orders.models import Side

CFG = GuardrailConfig()   # per_order_max=100000 · per_symbol_max_weight=0.30 · daily=500000


def ind(last: float) -> ScreenIndicators:
    return ScreenIndicators(last_close=last, sma_short=last, sma_long=last, rsi=50.0,
                            avg_volume=1_000_000.0)


def ctx(price: float = 10000.0, **kw) -> CandidateContext:
    base = dict(symbol="005930", name="삼성전자", market="KOSPI", currency="KRW",
                indicators=ind(price), score=0.05, already_held=False)
    base.update(kw)
    return CandidateContext(**base)


def dec(action: str = "BUY", conf: float = 1.0) -> Decision:
    return Decision(action=Action(action), symbol="005930", confidence=conf, rationale="t")


def test_hold_no_order():
    assert allocate(dec("HOLD"), ctx(), CFG) is None


def test_buy_per_order_max_ceiling():
    o = allocate(dec("BUY", 1.0), ctx(price=10000.0), CFG)   # 100000/10000 = 10
    assert o.side is Side.BUY and o.quantity == Decimal("10") and o.price == Decimal("10000")


def test_buy_confidence_scales():
    assert allocate(dec("BUY", 0.5), ctx(price=10000.0), CFG).quantity == Decimal("5")


def test_buy_cash_limit():
    o = allocate(dec("BUY", 1.0), ctx(price=10000.0, cash_buying_power_krw=Decimal("30000")), CFG)
    assert o.quantity == Decimal("3")


def test_buy_per_symbol_weight_limit():
    # portfolio 200000 · 비중 0.30 → 여유 60000 · price 10000 → 6주
    o = allocate(dec("BUY", 1.0), ctx(price=10000.0, portfolio_value_krw=Decimal("200000")), CFG)
    assert o.quantity == Decimal("6")


def test_buy_unaffordable_returns_none():
    # price 200000 > ceiling(100000) → 0주 → 주문 없음
    assert allocate(dec("BUY", 1.0), ctx(price=200000.0), CFG) is None


def test_buy_no_indicators_none():
    assert allocate(dec("BUY", 1.0), ctx(indicators=None), CFG) is None


def test_buy_notional_within_per_order_max():
    o = allocate(dec("BUY", 1.0), ctx(price=10000.0), CFG)
    assert o.estimated_notional() <= CFG.per_order_max_krw   # 가드레일 통과 보장


def test_sell_held_liquidates_full():
    o = allocate(dec("SELL", 0.9),
                 ctx(price=10000.0, already_held=True, held_quantity=Decimal("7")), CFG)
    assert o.side is Side.SELL and o.quantity == Decimal("7") and o.price == Decimal("10000")


def test_sell_unheld_none():
    assert allocate(dec("SELL", 0.9), ctx(already_held=False), CFG) is None
