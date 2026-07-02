"""페이퍼 포트폴리오 테스트 — 체결 수학(넷)·취득단가 블렌딩·실현손익·마킹·합성 Holdings."""

from __future__ import annotations

from decimal import Decimal

from app.engine.costs import CostConfig
from app.engine.paper import PaperPortfolio, PaperPosition
from app.orders.models import OrderRequest, OrderType, Side

D = Decimal
COST = CostConfig()   # 수수료 0.015bp=0.00015 · 슬리피지 0.0015 · 매도세 0.0015


def buy(symbol="005930", qty="10", price="10000") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=Side.BUY, order_type=OrderType.LIMIT,
                        quantity=D(qty), price=D(price))


def sell(symbol="005930", qty="10", price="11000") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=Side.SELL, order_type=OrderType.LIMIT,
                        quantity=D(qty), price=D(price))


def test_buy_fill_costs_and_avg_cost():
    p = PaperPortfolio(cash=D("1000000"))
    f = p.apply_fill(buy(), COST)
    fill_price = D("10000") * (1 + COST.slippage_rate)            # 10015
    total = D("10") * fill_price * (1 + COST.commission_rate)
    assert f.fill_price == fill_price and f.cash_delta == -total
    assert p.cash == D("1000000") - total
    assert p.positions["005930"].quantity == D("10")
    assert p.positions["005930"].avg_cost == total / 10           # 비용 포함 취득단가


def test_buy_blends_avg_cost():
    p = PaperPortfolio(cash=D("1000000"))
    p.apply_fill(buy(qty="10", price="10000"), COST)
    p.apply_fill(buy(qty="10", price="20000"), COST)
    pos = p.positions["005930"]
    assert pos.quantity == D("20")
    assert D("15000") < pos.avg_cost < D("15100")                 # 가중평균 + 비용

def test_sell_fill_realized_net_and_trade_count():
    p = PaperPortfolio(cash=D("0"),
                       positions={"005930": PaperPosition(quantity=D("10"), avg_cost=D("10000"))})
    f = p.apply_fill(sell(qty="10", price="11000"), COST)
    fill_price = D("11000") * (1 - COST.slippage_rate)
    proceeds = D("10") * fill_price * (1 - COST.commission_rate - COST.sell_tax_rate)
    assert f.cash_delta == proceeds and f.realized == proceeds - D("100000")
    assert p.cash == proceeds and p.realized_cum == f.realized
    assert p.trade_count == 1
    assert "005930" not in p.positions                            # 전량 청산 → 제거


def test_sell_partial_reduces_position():
    p = PaperPortfolio(cash=D("0"),
                       positions={"005930": PaperPosition(quantity=D("10"), avg_cost=D("10000"))})
    p.apply_fill(sell(qty="4", price="11000"), COST)
    assert p.positions["005930"].quantity == D("6")               # 부분 청산 지원


def test_buy_insufficient_cash_skipped():
    p = PaperPortfolio(cash=D("50000"))
    f = p.apply_fill(buy(qty="10", price="10000"), COST)          # ≈100,300 필요
    assert f.skipped and p.cash == D("50000") and not p.positions  # 상태 불변


def test_sell_unheld_skipped():
    p = PaperPortfolio(cash=D("0"))
    f = p.apply_fill(sell(), COST)
    assert f.skipped == "페이퍼 미보유" and p.trade_count == 0


def test_mark_equity_with_fallback():
    p = PaperPortfolio(cash=D("1000"),
                       positions={"A": PaperPosition(D("2"), D("100")),
                                  "B": PaperPosition(D("3"), D("50"))})
    equity, pv = p.mark_equity({"A": D("110")})                   # B 는 시세 없음 → 취득가 폴백
    assert pv == D("2") * 110 + D("3") * 50
    assert equity == D("1000") + pv


def test_synthetic_holdings_shape():
    p = PaperPortfolio(cash=D("0"),
                       positions={"005930": PaperPosition(D("2"), D("10000"))})
    h = p.to_synthetic_holdings({"005930": D("11000")})
    item = h.items[0]
    assert item.symbol == "005930" and item.quantity == D("2")
    assert item.average_purchase_price == D("10000") and item.last_price == D("11000")
    assert item.profit_loss.rate == D("0.1")                      # +10%
    assert h.market_value.amount.krw == D("22000")


def test_synthetic_holdings_empty():
    h = PaperPortfolio(cash=D("5000")).to_synthetic_holdings({})
    assert h.items == [] and h.market_value.amount.krw == D("0")
