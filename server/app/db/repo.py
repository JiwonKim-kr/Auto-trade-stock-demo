"""저장소 — 파이프라인/라우트 경계에서만 호출하는 DB I/O 파사드.

설계: **run_tick 은 DB 를 모른다**(순수 오케스트레이션 유지, entry_gate 와 같은 주입 철학).
라우트가 틱 전에 `buy_notional_today` 로 오늘 사용액을 읽어 run_tick 에 넘기고,
틱 후에 `record_tick`·`save_engine_state` 로 기록한다 — DB I/O 는 전부 경계에.

이로써 닫히는 안전 격차:
  - 일일 매수 한도가 **틱 내부에서만 누적**되던 구멍 → 오늘 전체(DB 합산)로 교차-틱 강제.
  - 킬스위치·서킷브레이커 래치가 재시작에 소실 → engine_state 로 생존.
  - clientOrderId 멱등이 인메모리뿐 → DB UNIQUE 2차 방어.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AuditRow, DecisionRow, EngineStateRow, OrderRow, TickRow
from app.orders.guardrails import KST
from app.orders.models import OrderResult, OrderStatus

if TYPE_CHECKING:  # 타입 전용(런타임 결합 회피) — db 층은 engine 을 모르는 게 원칙
    from app.engine.pipeline import TickResult

# 일일 매수 사용액에 포함할 상태: 의도된(DRY_RUN)·전송된(SUBMITTED) 매수만. REJECTED/FAILED 제외.
_USED_STATUSES = (OrderStatus.DRY_RUN.value, OrderStatus.SUBMITTED.value)


def trade_date_kst(dt: datetime) -> str:
    return dt.astimezone(KST).date().isoformat()


def _dec(s: str | None) -> Decimal:
    return Decimal(s) if s else Decimal(0)


class Repository:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    # ── 틱 기록 (틱 + 결정 + 주문 전수) ────────────────────────────────────────
    async def record_tick(self, result: "TickResult", started_at: datetime) -> int:
        async with self._sm() as s, s.begin():
            tick = TickRow(
                started_at=started_at,
                mode=result.mode,
                kill_switch=result.kill_switch,
                circuit_breaker=result.circuit_breaker,
                circuit_breaker_reason=result.circuit_breaker_reason,
                universe_count=len(result.universe_symbols),
                candidates=result.candidates,
                note=result.note,
                cost_gated_json=json.dumps(result.cost_gated, ensure_ascii=False),
            )
            s.add(tick)
            await s.flush()                      # tick.id 확보
            for d in result.decisions:
                s.add(DecisionRow(tick_id=tick.id, symbol=d.symbol, action=d.action.value,
                                  confidence=d.confidence, rationale=d.rationale))
            for o in result.orders:
                await self._add_order(s, o, tick.id)
            return tick.id

    async def _add_order(self, s, res: OrderResult, tick_id: int | None) -> None:
        if res.status is OrderStatus.DUPLICATE:
            return  # 멱등 재시도 에코 — 원본 행이 이미 있다
        exists = await s.scalar(
            select(OrderRow.id).where(OrderRow.client_order_id == res.client_order_id))
        if exists is not None:
            return  # UNIQUE 선검사(재기록 방지)
        req = res.request
        s.add(OrderRow(
            tick_id=tick_id,
            client_order_id=res.client_order_id,
            symbol=req.symbol,
            side=req.side.value,
            order_type=req.order_type.value,
            quantity=str(req.quantity) if req.quantity is not None else None,
            price=str(req.price) if req.price is not None else None,
            order_amount=str(req.order_amount) if req.order_amount is not None else None,
            time_in_force=req.time_in_force.value,
            mode=res.mode.value,
            status=res.status.value,
            reason=res.reason,
            toss_order_id=res.toss_order_id,
            created_at=res.created_at,
            trade_date=trade_date_kst(res.created_at),
        ))

    # ── 일일 매수 사용액 (교차-틱 일일 한도의 근거) ─────────────────────────────
    async def buy_notional_today(self, trade_date: str) -> Decimal:
        """해당 KST 날짜의 매수 명목합(의도+전송). Decimal 문자열을 Python 에서 정확 합산."""
        async with self._sm() as s:
            rows = (await s.execute(
                select(OrderRow.quantity, OrderRow.price, OrderRow.order_amount)
                .where(OrderRow.trade_date == trade_date,
                       OrderRow.side == "BUY",
                       OrderRow.status.in_(_USED_STATUSES))
            )).all()
        total = Decimal(0)
        for qty, price, amount in rows:
            total += _dec(amount) if amount else _dec(qty) * _dec(price)
        return total

    # ── 엔진 상태 (킬스위치·서킷브레이커 재시작 생존) ───────────────────────────
    async def load_engine_state(self) -> tuple[bool, dict] | None:
        async with self._sm() as s:
            row = await s.get(EngineStateRow, 1)
            if row is None:
                return None
            return row.kill_switch, json.loads(row.breaker_json or "{}")

    async def save_engine_state(self, kill_switch: bool, breaker: dict) -> None:
        async with self._sm() as s, s.begin():
            row = await s.get(EngineStateRow, 1)
            if row is None:
                row = EngineStateRow(id=1)
                s.add(row)
            row.kill_switch = kill_switch
            row.breaker_json = json.dumps(breaker, ensure_ascii=False)
            row.updated_at = datetime.now(timezone.utc)

    # ── 감사 (컨트롤플레인 이벤트) ─────────────────────────────────────────────
    async def audit(self, actor: str, action: str, payload: dict | None = None) -> None:
        async with self._sm() as s, s.begin():
            s.add(AuditRow(ts=datetime.now(timezone.utc), actor=actor, action=action,
                           payload_json=json.dumps(payload or {}, ensure_ascii=False)))
