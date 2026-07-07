"""SQLAlchemy 모델 — TECH-STACK §4 스키마 스케치의 1차 구현.

설계 결정:
  - **금액/수량은 정확한 10진 문자열(TEXT)로 저장.** Numeric 은 PG 에선 exact 지만 SQLite(로컬/테스트)
    에선 REAL(float)로 저장돼 테스트/운영 정밀도가 갈리고 "Decimal만, float 금지" 불변식을 깬다.
    합산은 조회 후 Python Decimal 로(일일 매수 사용액은 하루 주문 수가 한도로 유계라 부담 없음).
  - **trade_date(KST, YYYY-MM-DD) 별도 컬럼** — tz 저장 방언 차이를 피하고 일일 집계를 단순 인덱스로.
  - **orders.client_order_id UNIQUE** — 멱등 2차 방어(인메모리 _seen 이 1차, 재시작 후에도 DB가 차단).
  - **engine_state 단일행(id=1)** — 킬스위치·서킷브레이커 래치가 Cloud Run 재시작(min=0)에도 생존.
  - JSON 은 TEXT + json.dumps(방언 간 JSON 타입 차이 회피).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TickRow(Base):
    """틱 실행 1회 — 관측/감사의 뼈대(decisions/orders 가 매달림)."""

    __tablename__ = "ticks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trade_date: Mapped[str] = mapped_column(Text, default="")   # KST YYYY-MM-DD (일일 집계·비용가드)
    mode: Mapped[str] = mapped_column(Text)
    kill_switch: Mapped[bool] = mapped_column(default=False)
    circuit_breaker: Mapped[bool] = mapped_column(default=False)
    circuit_breaker_reason: Mapped[str] = mapped_column(Text, default="")
    universe_count: Mapped[int] = mapped_column(default=0)
    candidates: Mapped[int] = mapped_column(default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    cost_gated_json: Mapped[str] = mapped_column(Text, default="[]")
    regime_json: Mapped[str] = mapped_column(Text, default="{}")   # 레짐 판정(사이징 축소 근거 감사)


class DecisionRow(Base):
    """LLM/폴백 판단 — rationale 전수 로깅(감사·사후분석, TECH-STACK §5)."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tick_id: Mapped[int] = mapped_column(ForeignKey("ticks.id"))
    symbol: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)          # BUY/SELL/HOLD
    confidence: Mapped[float] = mapped_column()
    rationale: Mapped[str] = mapped_column(Text, default="")


class OrderRow(Base):
    """주문 원장(의도 DRY_RUN 포함 전수) — REJECTED 도 기록(가드레일 감사 가치)."""

    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_trade_date_side", "trade_date", "side"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tick_id: Mapped[int | None] = mapped_column(ForeignKey("ticks.id"), nullable=True)
    client_order_id: Mapped[str] = mapped_column(Text, unique=True)   # 멱등 2차 방어
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)             # BUY/SELL
    order_type: Mapped[str] = mapped_column(Text)       # LIMIT/MARKET
    quantity: Mapped[str | None] = mapped_column(Text, nullable=True)      # 정확 10진 문자열
    price: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_amount: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_in_force: Mapped[str] = mapped_column(Text, default="DAY")
    mode: Mapped[str] = mapped_column(Text)             # DRY_RUN/LIVE
    status: Mapped[str] = mapped_column(Text)           # DRY_RUN/REJECTED/SUBMITTED/FAILED
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    toss_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trade_date: Mapped[str] = mapped_column(Text)       # KST YYYY-MM-DD (일일 집계용)


class AuditRow(Base):
    """컨트롤플레인 감사 — 킬스위치 토글·모드 전환 등(주문은 orders 가 전수 보유)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(Text)            # "api" / "system"
    action: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class PositionSnapshotRow(Base):
    """포지션 스냅샷 헤더 — 리컨실 기준선. 보유 0종목도 스냅샷으로 성립(헤더 분리 이유)."""

    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # UTC 정규화(주문 created_at 과 비교)
    item_count: Mapped[int] = mapped_column(default=0)


class PositionRow(Base):
    """스냅샷 1종목분. 수량이 리컨실 대상, 평단가/통화는 감사·표시용."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("position_snapshots.id"), index=True)
    symbol: Mapped[str] = mapped_column(Text)
    quantity: Mapped[str] = mapped_column(Text)          # 정확 10진 문자열(소수점 주문 수용)
    avg_price: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaperStateRow(Base):
    """페이퍼 장부 헤더 단일행(id=1) — 현금·누적 실현손익·완결 왕복 수."""

    __tablename__ = "paper_state"

    id: Mapped[int] = mapped_column(primary_key=True)   # 항상 1
    cash: Mapped[str] = mapped_column(Text)             # 정확 10진 문자열
    realized_cum: Mapped[str] = mapped_column(Text, default="0")
    trade_count: Mapped[int] = mapped_column(default=0)
    seed: Mapped[str] = mapped_column(Text)             # 초기 자본(감사·수익률 기준)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaperPositionRow(Base):
    """페이퍼 포지션 현재 상태(스냅샷 아님 — save 시 전체 교체)."""

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(Text, unique=True)
    quantity: Mapped[str] = mapped_column(Text)
    avg_cost: Mapped[str] = mapped_column(Text)


class PaperEquityRow(Base):
    """페이퍼 자산곡선 1점(틱마다) — 평가 모듈 입력. benchmark 는 같은 시점 시장 프록시가."""

    __tablename__ = "paper_equity"
    __table_args__ = (Index("ix_paper_equity_trade_date", "trade_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trade_date: Mapped[str] = mapped_column(Text)       # KST YYYY-MM-DD
    equity: Mapped[str] = mapped_column(Text)
    cash: Mapped[str] = mapped_column(Text)
    positions_value: Mapped[str] = mapped_column(Text)
    realized_cum: Mapped[str] = mapped_column(Text)
    benchmark_price: Mapped[str | None] = mapped_column(Text, nullable=True)


class EngineStateRow(Base):
    """엔진 상태 단일행(id=1) — 킬스위치·서킷브레이커가 재시작에도 생존."""

    __tablename__ = "engine_state"

    id: Mapped[int] = mapped_column(primary_key=True)   # 항상 1
    kill_switch: Mapped[bool] = mapped_column(default=False)
    breaker_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
