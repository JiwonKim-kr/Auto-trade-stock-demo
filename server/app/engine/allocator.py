"""결정적 사이징 — Decision(+confidence) → OrderRequest 수량. LLM은 사이징하지 않는다.

- BUY: 한도들의 최소값(1주문 한도 · 현금매수여력 · 종목당 비중 여유)을 ceiling으로,
       `ceiling × confidence × 노출배수(레짐)`를 목표 금액으로 잡아 정수 주(KR) 수량 계산.
       1주 미만이면 주문 안 함. 노출배수는 [0,1] 클램프 — 레짐은 축소만 하지 레버리지를 키우지 않는다.
- SELL: 보유 전량 청산(보유 없으면 주문 없음). **노출배수 무관 — 청산 경로는 축소하지 않는다.**
- HOLD: 주문 없음.
가격 기준은 최근 종가(indicators.last_close). 결과 수량은 가드레일 한도 안에 들어오게 산정하되,
최종 강제는 주문층 가드레일이 한다(allocator는 '한도 안에서 제안', 가드레일은 '거부').
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.engine.llm import Action, CandidateContext, Decision
from app.orders.guardrails import GuardrailConfig
from app.orders.models import OrderRequest, OrderType, Side, TimeInForce


def _price(ctx: CandidateContext) -> Decimal | None:
    if ctx.indicators is None:
        return None
    price = Decimal(str(ctx.indicators.last_close))
    return price if price > 0 else None


def _buy_quantity(ctx: CandidateContext, decision: Decision, cfg: GuardrailConfig,
                  price: Decimal, exposure_multiplier: Decimal) -> Decimal:
    ceilings = [cfg.per_order_max_krw]
    if ctx.cash_buying_power_krw is not None:
        ceilings.append(ctx.cash_buying_power_krw)
    if ctx.portfolio_value_krw is not None:
        held_value = (ctx.held_quantity or Decimal(0)) * price
        # 비중 분모 = 총자산(포지션+현금). 포지션 평가액만 쓰면 빈 장부에서 여유가 0이 되어
        # 첫 매수가 영원히 불가능(스트레스 샌드박스가 발견한 콜드스타트 버그).
        equity = ctx.portfolio_value_krw + (ctx.cash_buying_power_krw or Decimal(0))
        room = cfg.per_symbol_max_weight * equity - held_value
        ceilings.append(room if room > 0 else Decimal(0))
    ceiling = min(ceilings)
    if ceiling <= 0:
        return Decimal(0)
    mult = min(max(exposure_multiplier, Decimal(0)), Decimal(1))   # 축소 전용(레버리지 금지)
    target = ceiling * Decimal(str(decision.confidence)) * mult
    return (target / price).to_integral_value(rounding=ROUND_DOWN)


def _req(side: Side, symbol: str, quantity: Decimal, price: Decimal,
         client_order_id: str | None) -> OrderRequest:
    kw: dict = dict(symbol=symbol, side=side, order_type=OrderType.LIMIT,
                    quantity=quantity, price=price, time_in_force=TimeInForce.DAY)
    if client_order_id is not None:
        kw["client_order_id"] = client_order_id
    return OrderRequest(**kw)


def allocate(decision: Decision, ctx: CandidateContext, config: GuardrailConfig,
             *, client_order_id: str | None = None,
             exposure_multiplier: Decimal = Decimal(1)) -> OrderRequest | None:
    """결정 → 주문 요청(없으면 None). 가격 산정 불가하면 주문 안 함.

    exposure_multiplier: 레짐 필터의 노출 배수(0~1). 매수 목표금액에만 곱한다 — 매도는 무관.
    """
    if decision.action is Action.HOLD:
        return None
    price = _price(ctx)
    if price is None:
        return None

    if decision.action is Action.SELL:
        if not ctx.already_held or not ctx.held_quantity or ctx.held_quantity <= 0:
            return None
        return _req(Side.SELL, ctx.symbol, ctx.held_quantity, price, client_order_id)

    # BUY
    qty = _buy_quantity(ctx, decision, config, price, exposure_multiplier)
    if qty < 1:
        return None
    return _req(Side.BUY, ctx.symbol, qty, price, client_order_id)
