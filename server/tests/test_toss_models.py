"""토스 응답 ↔ Pydantic 모델 매핑 회귀 테스트.

2026-06 라이브 실응답 픽스처(tests/fixtures/*.json)로 매핑을 고정한다.
토스 응답 형태가 바뀌면 여기서 깨져 조기에 잡는다. (인사이트: 추측 금지 · 실응답으로 확정)
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.toss.models import (
    Account,
    BuyingPower,
    Holdings,
    Price,
    Stock,
    TossEnvelope,
)

FIX = Path(__file__).parent / "fixtures"


def _result(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))["result"]


def test_accounts():
    accounts = [Account.model_validate(a) for a in _result("accounts.json")]
    a = accounts[0]
    assert a.account_seq == 1 and isinstance(a.account_seq, int)  # 헤더용 정수
    assert isinstance(a.account_no, str)
    assert a.account_type == "BROKERAGE"


def test_holdings_currency_model():
    h = Holdings.model_validate(_result("holdings.json"))

    # 루트 = 통화버킷 중첩 {krw, usd}
    assert h.total_purchase_amount.krw == Decimal("229000")
    assert h.total_purchase_amount.usd == Decimal("0.069972")
    assert h.market_value.amount.krw == Decimal("202500")
    assert h.market_value.amount.usd == Decimal("0.08014")

    # rate 는 분수 → ×100
    assert h.profit_loss.rate == Decimal("-0.1155")
    assert h.profit_loss.rate_percent == Decimal("-11.55")
    assert h.daily_profit_loss.rate == Decimal("-0.0938")

    # item = 평문 + item.currency
    kr, us = h.items[0], h.items[1]
    assert kr.symbol == "005935" and kr.currency == "KRW" and kr.market_country == "KR"
    assert kr.market_value.amount == Decimal("202500")        # 평문(원)
    assert kr.profit_loss.rate == Decimal("-0.1157")
    assert kr.cost.tax == Decimal("405")

    assert us.symbol == "AAPL" and us.currency == "USD" and us.market_country == "US"
    assert us.quantity == Decimal("0.000271")                 # 소수점 주문
    assert us.last_price == Decimal("295.72")
    assert us.cost.tax is None                                # 해외주 tax null


def test_buying_power():
    bp = BuyingPower.model_validate(_result("buying_power.json"))
    assert bp.currency == "KRW"
    assert bp.cash_buying_power == Decimal("0")


def test_prices():
    prices = [Price.model_validate(p) for p in _result("prices.json")]
    p = prices[0]
    assert p.symbol == "005930"
    assert p.last_price == Decimal("315500")
    assert p.currency == "KRW"
    assert p.timestamp.tzinfo is not None                     # tz-aware
    assert p.timestamp.year == 2026
    assert not hasattr(p, "volume")                           # 등락률/거래량 없음


def test_stocks_risk_flags():
    stocks = [Stock.model_validate(s) for s in _result("stocks.json")]
    s = stocks[0]
    assert s.symbol == "005930"
    assert s.is_common_share is True                          # 우선주 아님
    assert s.leverage_factor is None                          # 레버리지/인버스 아님
    assert s.status == "ACTIVE"
    assert s.security_type == "STOCK"
    assert s.list_date == date(1975, 6, 11)
    assert s.delist_date is None
    assert s.shares_outstanding == Decimal("5846278608")
    md = s.korean_market_detail
    assert md.liquidation_trading is False                    # 정리매매 아님
    assert md.krx_trading_suspended is False                  # 거래정지 아님


def test_warnings_empty():
    assert _result("warnings.json") == []                     # 스모크 관측: 빈 배열


def test_envelope_generic():
    raw = json.loads((FIX / "accounts.json").read_text(encoding="utf-8"))
    env = TossEnvelope[list[Account]].model_validate(raw)
    assert env.result[0].account_seq == 1
