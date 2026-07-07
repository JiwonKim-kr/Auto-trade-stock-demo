"""페이퍼 포트폴리오 — DRY_RUN 의도 주문을 모의 체결해 전략 P&L 을 넷(net)으로 추적.

목적: DRY_RUN 은 실체결이 없어 손익이 없다 → "이 시스템이 실제로 돌았다면?" 을 측정하려면
모의 장부가 필요하다. 핵심은 **자기일관(self-consistent) 루프**:
  - 파이프라인의 '보유'를 **페이퍼 포지션**으로(→ LLM 이 페이퍼 보유를 매도 평가),
  - 사이징의 현금을 **페이퍼 현금**으로(→ 가드레일·비중도 페이퍼 장부 기준).
  이렇게 해야 매수만 쌓이지 않고 진입→청산의 완결 루프가 측정된다. (라우트가 조립: repo 상태
  → 시세 마킹 → 합성 Holdings → run_tick → 모의 체결 → 저장·자산곡선 기록.)

체결 가정(문서화된 한계):
  - 지정가(=최근 종가) **즉시 전량 체결** + 슬리피지 불리 방향 적용(매수 ↑·매도 ↓)
    → 체결 확실성은 낙관적, 가격은 보수적. 미체결/부분체결 모형은 향후 정밀화. (TODO)
  - 비용은 CostConfig 재사용(진입 게이트와 동일 모델): 수수료 양방향·매도세는 매도만.
  - 매수 비용은 취득원가(avg_cost)에 산입, 실현손익은 매도 시 넷으로 확정(study.md §3.4 넷 원칙).

trade_count 는 **매도(청산) 발생 횟수** = 완결 왕복 수 — 평가 모듈의 표본 게이트(N<100 판단 보류) 입력.
모든 금액은 Decimal(float 금지). 저장은 db.repo(정확 문자열), 이 모듈은 순수 계산만.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.engine.costs import CostConfig
from app.orders.models import OrderRequest, Side
from app.toss.models import Holdings


@dataclass
class PaperPosition:
    quantity: Decimal
    avg_cost: Decimal                      # 매수 비용 포함 취득단가
    opened_at: datetime | None = None      # 최초 진입 시각(타임스톱 기준 — 추가매수에도 유지)


@dataclass
class PaperFill:
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    cash_delta: Decimal                    # 매수 − / 매도 +
    realized: Decimal | None = None        # 매도 시 넷 실현손익
    skipped: str | None = None             # 체결 불가 사유(현금 부족 등)

    def as_dict(self) -> dict:
        d = {"symbol": self.symbol, "side": self.side, "quantity": str(self.quantity),
             "fill_price": str(self.fill_price), "cash_delta": str(self.cash_delta)}
        if self.realized is not None:
            d["realized"] = str(self.realized)
        if self.skipped:
            d["skipped"] = self.skipped
        return d


@dataclass
class PaperPortfolio:
    cash: Decimal
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    realized_cum: Decimal = Decimal(0)     # 누적 실현손익(넷)
    trade_count: int = 0                   # 완결 왕복(매도) 수 — 평가 표본 게이트 입력

    # ── 모의 체결 ─────────────────────────────────────────────────────────────
    def apply_fill(self, req: OrderRequest, cost: CostConfig,
                   now: datetime | None = None) -> PaperFill | None:
        """DRY_RUN 의도 주문 1건 모의 체결. 가격/수량 없으면 None(체결 불가).

        now: 신규 매수 포지션의 opened_at(타임스톱 기준). 미전달 시 opened_at 없음(타임스톱 미적용).
        """
        if req.price is None or req.quantity is None or req.quantity <= 0:
            return None
        if req.side is Side.BUY:
            return self._fill_buy(req.symbol, req.quantity, req.price, cost, now)
        return self._fill_sell(req.symbol, req.quantity, req.price, cost)

    def _fill_buy(self, symbol: str, qty: Decimal, price: Decimal, cost: CostConfig,
                  now: datetime | None) -> PaperFill:
        fill_price = price * (1 + cost.slippage_rate)          # 슬리피지 불리 방향
        gross = qty * fill_price
        total_cost = gross * (1 + cost.commission_rate)        # 수수료 포함 취득원가
        if total_cost > self.cash:                             # 방어(사이징이 넘긴 희귀 케이스)
            return PaperFill(symbol, "BUY", qty, fill_price, Decimal(0),
                             skipped=f"현금 부족({total_cost} > {self.cash})")
        self.cash -= total_cost
        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = PaperPosition(quantity=qty, avg_cost=total_cost / qty,
                                                   opened_at=now)
        else:                                                  # 취득원가 가중 평균(opened_at 유지)
            new_qty = pos.quantity + qty
            pos.avg_cost = (pos.quantity * pos.avg_cost + total_cost) / new_qty
            pos.quantity = new_qty
        return PaperFill(symbol, "BUY", qty, fill_price, -total_cost)

    def _fill_sell(self, symbol: str, qty: Decimal, price: Decimal, cost: CostConfig) -> PaperFill:
        pos = self.positions.get(symbol)
        if pos is None or pos.quantity <= 0:
            return PaperFill(symbol, "SELL", qty, price, Decimal(0), skipped="페이퍼 미보유")
        qty = min(qty, pos.quantity)
        fill_price = price * (1 - cost.slippage_rate)
        gross = qty * fill_price
        proceeds = gross * (1 - cost.commission_rate - cost.sell_tax_rate)   # 넷 수취
        realized = proceeds - qty * pos.avg_cost
        self.cash += proceeds
        self.realized_cum += realized
        self.trade_count += 1
        pos.quantity -= qty
        if pos.quantity == 0:
            del self.positions[symbol]
        return PaperFill(symbol, "SELL", qty, fill_price, proceeds, realized=realized)

    # ── 평가(마킹) ────────────────────────────────────────────────────────────
    def _mark(self, symbol: str, marks: dict[str, Decimal]) -> Decimal:
        """마킹 가격: 시세 없으면 취득단가 폴백(정지/조회 실패 — 곡선 왜곡보다 보수적 유지)."""
        return marks.get(symbol) or self.positions[symbol].avg_cost

    def mark_equity(self, marks: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
        """(총자산, 포지션 평가액). 총자산 = 현금 + Σ 수량×마킹가."""
        positions_value = sum(
            (p.quantity * self._mark(s, marks) for s, p in self.positions.items()), Decimal(0))
        return self.cash + positions_value, positions_value

    def to_synthetic_holdings(self, marks: dict[str, Decimal]) -> Holdings:
        """파이프라인용 합성 Holdings — LLM/가드레일이 페이퍼 장부를 '보유'로 보게 한다."""
        items = []
        total_purchase = total_value = Decimal(0)
        for symbol, pos in sorted(self.positions.items()):
            mark = self._mark(symbol, marks)
            purchase = pos.quantity * pos.avg_cost
            value = pos.quantity * mark
            pl = value - purchase
            rate = pl / purchase if purchase > 0 else Decimal(0)
            total_purchase += purchase
            total_value += value
            items.append({
                "symbol": symbol, "name": symbol, "currency": "KRW",
                "quantity": str(pos.quantity), "lastPrice": str(mark),
                "averagePurchasePrice": str(pos.avg_cost),
                "marketValue": {"purchaseAmount": str(purchase), "amount": str(value)},
                "profitLoss": {"amount": str(pl), "rate": str(rate)},
            })
        total_pl = total_value - total_purchase
        total_rate = total_pl / total_purchase if total_purchase > 0 else Decimal(0)
        return Holdings.model_validate({
            "totalPurchaseAmount": {"krw": str(total_purchase)},
            "marketValue": {"amount": {"krw": str(total_value)}},
            "profitLoss": {"amount": {"krw": str(total_pl)}, "rate": str(total_rate)},
            "items": items,
        })
