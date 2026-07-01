"""하드 가드레일 — 모드(DRY_RUN/LIVE) 무관하게 주문 진입 시 선검사.

인사이트 §5/§6: 킬스위치 · 1주문 최대 금액 · 일일 매수 한도 · 종목당 비중 · 최대 포지션 수
· KRX 장시간 게이트. LLM 이 넘을 수 없는 결정적 안전선이다.

한도 통화: KRW 기준. (해외주 FX 정규화는 추후 환율 적용으로 보강 — TODO)
휴장일: 여기서는 주말+장시간만 검사. 공휴일은 market-calendar/KR 연동으로 보강 — TODO.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel

from app.orders.models import OrderRequest, Side

# 한국은 1988년 이후 DST 없음 → 고정 +09:00 (tzdata 의존 회피)
KST = timezone(timedelta(hours=9))


class GuardrailConfig(BaseModel):
    per_order_max_krw: Decimal = Decimal("100000")      # 1주문 최대 금액
    daily_buy_cap_krw: Decimal = Decimal("500000")      # 일일 매수 한도
    max_positions: int = 10                             # 최대 보유 종목 수
    per_symbol_max_weight: Decimal = Decimal("0.10")    # 종목당 최대 비중(0~1). max_positions=10과 정합(10×10%=완전배포 가능·단일종목 집중 차단)
    market_open: time = time(9, 0)                      # KST
    market_close: time = time(15, 30)                   # KST
    enforce_market_hours: bool = True


@dataclass
class GuardrailContext:
    """가드레일 평가에 필요한 런타임 상태. (실사용 시 holdings/DB 에서 채운다)"""

    now: datetime                                       # tz-aware 권장
    kill_switch: bool = False
    open_positions: int = 0
    held_symbols: frozenset[str] = frozenset()
    daily_buy_used_krw: Decimal = Decimal(0)
    portfolio_value_krw: Decimal | None = None          # None 이면 비중 검사 스킵
    symbol_current_value_krw: Decimal = Decimal(0)
    circuit_breaker_halt: bool = False                  # 서킷브레이커 발동 → 신규 매수만 차단
    circuit_breaker_reason: str = ""


@dataclass
class Violation:
    code: str
    reason: str


def _is_buy(order: OrderRequest) -> bool:
    return order.side is Side.BUY


def guard_kill_switch(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if ctx.kill_switch:
        return Violation("KILL_SWITCH", "킬스위치 작동 중 — 모든 주문 차단")
    return None


def guard_circuit_breaker(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    """손실 서킷브레이커: 발동 시 **신규 매수만** 차단(매도=청산은 항상 허용)."""
    if not _is_buy(order):
        return None
    if ctx.circuit_breaker_halt:
        return Violation("CIRCUIT_BREAKER", ctx.circuit_breaker_reason or "서킷브레이커 발동 — 신규 진입 중단")
    return None


def guard_market_hours(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if not cfg.enforce_market_hours:
        return None
    n = ctx.now.astimezone(KST)
    if n.weekday() >= 5:
        return Violation("MARKET_CLOSED", f"주말(KST {n:%a}) — KRX 휴장")
    if not (cfg.market_open <= n.time() <= cfg.market_close):
        return Violation(
            "MARKET_CLOSED",
            f"장시간 외 (KST {n:%H:%M}, 허용 {cfg.market_open:%H:%M}-{cfg.market_close:%H:%M})",
        )
    return None


def guard_per_order_max(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if not _is_buy(order):
        return None
    notional = order.estimated_notional()
    if notional is None:
        return Violation("UNBOUNDED_COST", "매수 비용 추정 불가(price/orderAmount 없음) — 안전상 차단")
    if notional > cfg.per_order_max_krw:
        return Violation("PER_ORDER_MAX", f"1주문 한도 초과: {notional} > {cfg.per_order_max_krw}")
    return None


def guard_daily_buy_cap(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if not _is_buy(order):
        return None
    notional = order.estimated_notional() or Decimal(0)
    total = ctx.daily_buy_used_krw + notional
    if total > cfg.daily_buy_cap_krw:
        return Violation("DAILY_BUY_CAP", f"일일 매수 한도 초과: {total} > {cfg.daily_buy_cap_krw}")
    return None


def guard_max_positions(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if not _is_buy(order):
        return None
    if order.symbol in ctx.held_symbols:
        return None  # 기존 종목 추가매수는 포지션 수 불변
    if ctx.open_positions + 1 > cfg.max_positions:
        return Violation(
            "MAX_POSITIONS", f"최대 보유 종목 수 초과: {ctx.open_positions + 1} > {cfg.max_positions}"
        )
    return None


def guard_per_symbol_weight(order: OrderRequest, ctx: GuardrailContext, cfg: GuardrailConfig):
    if not _is_buy(order):
        return None
    if ctx.portfolio_value_krw is None:
        return None  # 포트폴리오 총액 모르면 스킵
    notional = order.estimated_notional() or Decimal(0)
    resulting = ctx.symbol_current_value_krw + notional
    denom = ctx.portfolio_value_krw + notional
    if denom <= 0:
        return None
    weight = resulting / denom
    if weight > cfg.per_symbol_max_weight:
        return Violation(
            "PER_SYMBOL_WEIGHT",
            f"종목 비중 초과: {weight * 100:.2f}% > {cfg.per_symbol_max_weight * 100:.2f}%",
        )
    return None


ALL_GUARDRAILS = [
    guard_kill_switch,
    guard_circuit_breaker,
    guard_market_hours,
    guard_per_order_max,
    guard_daily_buy_cap,
    guard_max_positions,
    guard_per_symbol_weight,
]


def run_guardrails(
    order: OrderRequest,
    ctx: GuardrailContext,
    cfg: GuardrailConfig,
    guardrails=None,
) -> list[Violation]:
    """모든 가드레일을 돌려 위반 목록을 반환. 비어 있으면 통과."""
    gs = guardrails or ALL_GUARDRAILS
    return [v for g in gs if (v := g(order, ctx, cfg)) is not None]
