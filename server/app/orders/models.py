"""주문 요청/결과 모델.

토스 POST /api/v1/orders 바디 (인사이트 §2.4):
  clientOrderId(멱등키) · symbol · side(BUY/SELL) · orderType(LIMIT/MARKET)
  · quantity · price · orderAmount · timeInForce(DAY/CLS).
식별자는 stockCode 가 아니라 **symbol**.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(str, Enum):
    DAY = "DAY"
    CLS = "CLS"


class TradingMode(str, Enum):
    DRY_RUN = "DRY_RUN"   # 기본 — 실 POST /orders 미호출
    LIVE = "LIVE"         # 실자금 전송


class OrderStatus(str, Enum):
    DRY_RUN = "DRY_RUN"       # 의도된 주문, 미전송
    REJECTED = "REJECTED"     # 가드레일 차단
    SUBMITTED = "SUBMITTED"   # 토스로 전송됨(LIVE)
    FAILED = "FAILED"         # LIVE 전송 실패
    DUPLICATE = "DUPLICATE"   # 멱등 재시도 — 이전 결과 반환(재전송 안 함)


def new_client_order_id() -> str:
    """멱등키 기본값. 재시도 시 중복주문을 막으려면 호출자가 **안정적인 id**를 직접 지정한다."""
    return "ord_" + uuid4().hex


class OrderRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    client_order_id: str = Field(default_factory=new_client_order_id)
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal | None = None
    price: Decimal | None = None
    order_amount: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY

    def estimated_notional(self) -> Decimal | None:
        """매수 비용 추정. orderAmount 우선, 없으면 quantity×price. 둘 다 없으면 None(=경계 불명)."""
        if self.order_amount is not None:
            return self.order_amount
        if self.quantity is not None and self.price is not None:
            return self.quantity * self.price
        return None

    def to_toss_body(self) -> dict:
        """토스 전송용 바디(camelCase, None 제외). 실제 전송은 LIVE executor 에서만."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class OrderResult(BaseModel):
    client_order_id: str
    status: OrderStatus
    mode: TradingMode
    request: OrderRequest
    reason: str | None = None
    toss_order_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def sent_to_market(self) -> bool:
        """실제 시장에 전송됐는가(LIVE 제출 성공만 True)."""
        return self.status is OrderStatus.SUBMITTED
