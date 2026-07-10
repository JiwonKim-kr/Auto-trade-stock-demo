"""스트레스 샌드박스 회귀 테스트 — 안전장치 체인의 손실 한정·CB 최후 방어선 증명을 CI 에 고정."""

from __future__ import annotations

from decimal import Decimal

from app.engine.allocator import allocate
from app.engine.llm import Action, CandidateContext, Decision
from app.engine.screener import ScreenIndicators
from app.engine.stress import SCENARIOS, simulate
from app.orders.guardrails import GuardrailConfig

CRASH, GAP = SCENARIOS[0], SCENARIOS[1]


async def test_full_defense_bounds_losses_in_crash():
    r = await simulate(CRASH)
    assert r.buys_total > 0                          # 진입이 실제로 일어났고
    assert r.forced_exits > 0                        # 손절이 발동했으며
    assert r.max_drawdown < 0.15                     # 층별 흡수로 CB 문턱(-15%) 미만에서 방어
    assert r.buys_after_trip == 0


async def test_minimal_defense_circuit_breaker_is_last_resort():
    r = await simulate(CRASH, minimal_defenses=True)
    assert r.cb_tripped_day is not None              # 최후 방어선 실발동
    assert r.buys_after_trip == 0                    # 발동 후 신규 진입 0 (핵심 불변식)
    assert r.max_drawdown < 0.40                     # 발동 지연 포함 상한(관측 31%)


async def test_gap_down_minimal_defense():
    r = await simulate(GAP, minimal_defenses=True)
    assert r.cb_tripped_day is not None and r.buys_after_trip == 0


def test_allocator_cold_start_buys_with_cash_only():
    # 샌드박스가 발견한 버그의 회귀 고정: 빈 장부(포지션 0)여도 현금 기준 비중으로 첫 매수 가능
    ctx = CandidateContext(
        symbol="005930", name="삼성전자", market="KOSPI", currency="KRW",
        indicators=ScreenIndicators(last_close=10000.0, sma_short=1, sma_long=1, rsi=50,
                                    avg_volume=1e6),
        score=0.05, already_held=False,
        portfolio_value_krw=Decimal("0"), cash_buying_power_krw=Decimal("1000000"))
    d = Decision(action=Action.BUY, symbol="005930", confidence=1.0, rationale="t")
    order = allocate(d, ctx, GuardrailConfig())
    assert order is not None and order.quantity == Decimal("10")   # 10% × 1,000,000 / 10,000