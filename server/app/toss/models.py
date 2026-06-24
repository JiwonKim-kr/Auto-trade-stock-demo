"""토스 Open API 응답 Pydantic 모델 — 2026-06 라이브 실응답으로 확정.

설계 불변식 (인사이트 §2.4 / 스모크 실측):
  - 금액·수량·가격은 전부 **문자열로 오므로 `Decimal` 로 받는다** (float 금지).
  - **루트(holdings) 금액 = 통화버킷 중첩** `{krw, usd}` (같은 돈의 환산 아님 — 통화별 보유 분리).
  - **item 금액 = 그 종목 통화의 평문 문자열** + `item.currency` 로 통화 식별.
  - `profitLoss.rate` 는 **분수** ("-0.1155" = -11.55%) → 표시 시 ×100 (`rate_percent`).
  - 응답은 `{ "result": ... }` 로 감싸여 온다 → `TossEnvelope` 또는 클라이언트에서 unwrap.

필드명: JSON 은 camelCase, 파이썬은 snake_case (`alias_generator=to_camel`).
extra="ignore": 토스가 필드를 추가해도 깨지지 않게(운영 내성). 새 필드 포착이 필요하면 픽스처 테스트로.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, RootModel
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class TossModel(BaseModel):
    """모든 토스 모델의 베이스: camelCase 별칭 + 입력 내성."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


class TossEnvelope(BaseModel, Generic[T]):
    """`{ "result": <T> }` 공통 래퍼. 예: `TossEnvelope[list[Account]]`."""

    model_config = ConfigDict(populate_by_name=True)
    result: T


# ── 통화 버킷 (루트 금액 전용) ────────────────────────────────────────────────
class CurrencyBucket(RootModel[dict[str, Decimal]]):
    """통화별 금액 버킷. 예: {'krw': 229000, 'usd': 0.069972}.

    ⚠️ 같은 돈을 두 통화로 표기한 게 아니라 **통화별 보유분 분리**. 합산하려면 환율 환산 필요.
    """

    def get(self, currency: str) -> Decimal:
        return self.root.get(currency.lower(), Decimal(0))

    @property
    def krw(self) -> Decimal:
        return self.get("krw")

    @property
    def usd(self) -> Decimal:
        return self.get("usd")


# ── 계좌 ────────────────────────────────────────────────────────────────────
class Account(TossModel):
    account_no: str          # 계좌번호 (헤더에 넣으면 400 — 쓰지 말 것)
    account_seq: int         # ★ X-Tossinvest-Account 에 넣는 정수 식별자
    account_type: str        # "BROKERAGE"


# ── 보유(holdings) ──────────────────────────────────────────────────────────
class RootMarketValue(TossModel):
    amount: CurrencyBucket
    amount_after_cost: CurrencyBucket | None = None


class RootProfitLoss(TossModel):
    amount: CurrencyBucket
    amount_after_cost: CurrencyBucket | None = None
    rate: Decimal                                   # 분수
    rate_after_cost: Decimal | None = None

    @property
    def rate_percent(self) -> Decimal:
        return self.rate * 100


class RootDailyProfitLoss(TossModel):
    amount: CurrencyBucket
    rate: Decimal

    @property
    def rate_percent(self) -> Decimal:
        return self.rate * 100


class ItemMarketValue(TossModel):
    purchase_amount: Decimal
    amount: Decimal
    amount_after_cost: Decimal | None = None


class ItemProfitLoss(TossModel):
    amount: Decimal
    amount_after_cost: Decimal | None = None
    rate: Decimal                                   # 분수
    rate_after_cost: Decimal | None = None

    @property
    def rate_percent(self) -> Decimal:
        return self.rate * 100


class ItemDailyProfitLoss(TossModel):
    amount: Decimal
    rate: Decimal

    @property
    def rate_percent(self) -> Decimal:
        return self.rate * 100


class HoldingCost(TossModel):
    commission: Decimal | None = None
    tax: Decimal | None = None                      # 해외주 등에서 null 가능


class HoldingItem(TossModel):
    symbol: str
    name: str
    market_country: str | None = None               # "KR" / "US"
    currency: str                                   # ★ 이 item 금액의 통화 ("KRW"/"USD")
    quantity: Decimal                               # 소수점 주문 가능 (예: 0.000271)
    last_price: Decimal
    average_purchase_price: Decimal
    market_value: ItemMarketValue                   # 평문(이 종목 통화)
    profit_loss: ItemProfitLoss                     # 평문
    daily_profit_loss: ItemDailyProfitLoss | None = None
    cost: HoldingCost | None = None


class Holdings(TossModel):
    total_purchase_amount: CurrencyBucket           # 루트 = 통화버킷 중첩
    market_value: RootMarketValue
    profit_loss: RootProfitLoss
    daily_profit_loss: RootDailyProfitLoss | None = None
    items: list[HoldingItem] = []


# ── 매수가능금액 ──────────────────────────────────────────────────────────────
class BuyingPower(TossModel):
    currency: str
    cash_buying_power: Decimal


# ── 현재가 ────────────────────────────────────────────────────────────────────
class Price(TossModel):
    symbol: str
    timestamp: datetime                             # ISO8601 +09:00 (tz-aware)
    last_price: Decimal
    currency: str
    # ⚠️ 등락률/거래량 없음 → candles/trades 로 별도 취득


# ── 종목 마스터 ───────────────────────────────────────────────────────────────
class KoreanMarketDetail(TossModel):
    liquidation_trading: bool                       # 정리매매
    nxt_supported: bool | None = None
    krx_trading_suspended: bool                     # 거래정지
    nxt_trading_suspended: bool | None = None


class Stock(TossModel):
    symbol: str
    name: str
    english_name: str | None = None
    isin_code: str | None = None
    market: str | None = None                       # "KOSPI"
    security_type: str | None = None                # "STOCK" (SPAC/ETN 판정 보조)
    is_common_share: bool | None = None             # False = 우선주
    status: str | None = None                       # "ACTIVE" (그 외 = 비활성)
    currency: str | None = None
    list_date: date | None = None
    delist_date: date | None = None
    shares_outstanding: Decimal | None = None
    leverage_factor: Decimal | None = None          # null 아니면 레버리지/인버스
    korean_market_detail: KoreanMarketDetail | None = None
    # ⚠️ 섹터 정보 없음


# ── 캔들(시세 봉) ─────────────────────────────────────────────────────────────
# GET /api/v1/candles?symbol=...&interval=1d → result.candles[] (2026-06 신규 실측).
# 가격·거래량 모두 문자열. /prices 와 달리 **거래량 있음**. API는 최신→과거 순으로 준다.
class Candle(TossModel):
    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str


class CandleSeries(TossModel):
    candles: list[Candle] = []


# ── 종목별 경고 ───────────────────────────────────────────────────────────────
# 스모크에서 result=[] 만 관측 → 채워진 형태 미확정. 관측되면 모델 보강.
class StockWarning(TossModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")
