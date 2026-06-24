"""holdings(토스) → GuardrailContext 변환 — 클라이언트와 가드레일을 잇는 다리.

포트폴리오 총액은 KRW 버킷 기준(해외분은 별도 통화버킷 — FX 정규화는 추후 보강).
일일 매수 사용액은 DB 집계가 붙기 전까지 호출자가 주입(기본 0).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.orders.guardrails import GuardrailContext
from app.toss.models import Holdings


def context_from_holdings(
    holdings: Holdings,
    now: datetime,
    *,
    kill_switch: bool = False,
    daily_buy_used_krw: Decimal = Decimal(0),
    symbol_current_value_krw: Decimal = Decimal(0),
) -> GuardrailContext:
    return GuardrailContext(
        now=now,
        kill_switch=kill_switch,
        open_positions=len(holdings.items),
        held_symbols=frozenset(i.symbol for i in holdings.items),
        daily_buy_used_krw=daily_buy_used_krw,
        portfolio_value_krw=holdings.market_value.amount.krw,
        symbol_current_value_krw=symbol_current_value_krw,
    )
