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

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    AuditRow,
    CandleCacheRow,
    DecisionRow,
    EngineStateRow,
    OrderRow,
    PaperEquityRow,
    PaperPositionRow,
    PaperStateRow,
    NewsLabelRow,
    NewsModelOutputRow,
    NewsRow,
    PositionRow,
    PositionSnapshotRow,
    ResearchCacheRow,
    ReportLogRow,
    SymbolStatsRow,
    TickRow,
)
from app.engine.paper import PaperPortfolio, PaperPosition
from app.orders.guardrails import KST
from app.orders.models import OrderResult, OrderStatus
from app.orders.reconcile import PositionSnapshot

if TYPE_CHECKING:  # 타입 전용(런타임 결합 회피) — db 층은 engine 을 모르는 게 원칙
    from app.engine.pipeline import TickResult

# 일일 매수 사용액에 포함할 상태: 의도된(DRY_RUN)·전송된(SUBMITTED) 매수만. REJECTED/FAILED 제외.
_USED_STATUSES = (OrderStatus.DRY_RUN.value, OrderStatus.SUBMITTED.value)


def trade_date_kst(dt: datetime) -> str:
    return dt.astimezone(KST).date().isoformat()


def _dec(s: str | None) -> Decimal:
    return Decimal(s) if s else Decimal(0)


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite 는 tz 를 보존하지 않는다(naive UTC 벽시각으로 돌아옴) → 로드 시 UTC 재부착.
    naive 인 채로 astimezone 을 부르면 로컬(KST)로 오인해 9시간 시프트되는 버그 방지."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Repository:
    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    # ── 틱 기록 (틱 + 결정 + 주문 전수) ────────────────────────────────────────
    async def record_tick(self, result: "TickResult", started_at: datetime) -> int:
        async with self._sm() as s, s.begin():
            tick = TickRow(
                started_at=started_at,
                trade_date=trade_date_kst(started_at),
                mode=result.mode,
                kill_switch=result.kill_switch,
                circuit_breaker=result.circuit_breaker,
                circuit_breaker_reason=result.circuit_breaker_reason,
                universe_count=len(result.universe_symbols),
                candidates=result.candidates,
                note=result.note,
                cost_gated_json=json.dumps(result.cost_gated, ensure_ascii=False),
                regime_json=json.dumps(result.regime, ensure_ascii=False),
            )
            s.add(tick)
            await s.flush()                      # tick.id 확보
            for d in result.decisions:
                s.add(DecisionRow(
                    tick_id=tick.id, symbol=d.symbol, action=d.action.value,
                    confidence=d.confidence, rationale=d.rationale,
                    decision_price=str(d.decision_price) if d.decision_price is not None else None))
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

    # ── 카운트 (유니버스 로테이션·LLM 비용가드 입력) ────────────────────────────
    async def count_ticks(self) -> int:
        """누적 틱 수 — 유니버스 코호트 로테이션 오프셋(틱수 × limit)의 근거."""
        async with self._sm() as s:
            return (await s.scalar(select(func.count(TickRow.id)))) or 0

    async def count_decisions_today(self, trade_date: str) -> int:
        """오늘 기록된 판단 수 — 일일 LLM 비용 상한 판정(근사: 폴백 판단 포함)."""
        async with self._sm() as s:
            return (await s.scalar(
                select(func.count(DecisionRow.id))
                .join(TickRow, DecisionRow.tick_id == TickRow.id)
                .where(TickRow.trade_date == trade_date))) or 0

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

    # ── 포지션 스냅샷 (리컨실 기준선) ──────────────────────────────────────────
    async def save_positions_snapshot(self, ts: datetime, items: list[PositionSnapshot]) -> int:
        """현재 보유를 스냅샷으로 저장(0종목도 성립). ts 는 UTC 정규화(주문 created_at 과 비교)."""
        async with self._sm() as s, s.begin():
            head = PositionSnapshotRow(ts=ts.astimezone(timezone.utc), item_count=len(items))
            s.add(head)
            await s.flush()
            for it in items:
                s.add(PositionRow(snapshot_id=head.id, symbol=it.symbol,
                                  quantity=str(it.quantity),
                                  avg_price=str(it.avg_price) if it.avg_price is not None else None,
                                  currency=it.currency))
            return head.id

    async def load_latest_positions(self) -> tuple[datetime, dict[str, Decimal]] | None:
        """최신 스냅샷 → (ts, {symbol: 수량}). 없으면 None(첫 실행 = 기준선 생성)."""
        async with self._sm() as s:
            head = (await s.execute(
                select(PositionSnapshotRow).order_by(PositionSnapshotRow.id.desc()).limit(1)
            )).scalars().first()
            if head is None:
                return None
            rows = (await s.execute(
                select(PositionRow).where(PositionRow.snapshot_id == head.id))).scalars().all()
            return _utc(head.ts), {r.symbol: Decimal(r.quantity) for r in rows}

    async def submitted_qty_since(self, ts: datetime) -> dict[str, Decimal]:
        """ts 이후 전송(SUBMITTED) 주문의 심볼별 순증감(매수+·매도−) — 기대 수량 근사.

        ⚠️ 전송 기준(체결 아님): 미체결/부분체결은 불일치로 뜬다(보수적 오탐). 체결 API 연동 시 정밀화.
        """
        async with self._sm() as s:
            rows = (await s.execute(
                select(OrderRow.symbol, OrderRow.side, OrderRow.quantity)
                .where(OrderRow.status == OrderStatus.SUBMITTED.value,
                       OrderRow.created_at > ts.astimezone(timezone.utc))
            )).all()
        delta: dict[str, Decimal] = {}
        for symbol, side, qty in rows:
            if not qty:
                continue
            sign = Decimal(1) if side == "BUY" else Decimal(-1)
            delta[symbol] = delta.get(symbol, Decimal(0)) + sign * Decimal(qty)
        return delta

    # ── 페이퍼 장부 (DRY_RUN 모의 체결 상태 + 자산곡선) ─────────────────────────
    async def load_paper(self) -> PaperPortfolio | None:
        """페이퍼 장부 복원. 없으면 None(첫 실행 — 라우트가 seed 로 초기화)."""
        async with self._sm() as s:
            state = await s.get(PaperStateRow, 1)
            if state is None:
                return None
            rows = (await s.execute(select(PaperPositionRow))).scalars().all()
        return PaperPortfolio(
            cash=Decimal(state.cash),
            positions={r.symbol: PaperPosition(quantity=Decimal(r.quantity),
                                               avg_cost=Decimal(r.avg_cost),
                                               opened_at=_utc(r.opened_at)) for r in rows},
            realized_cum=Decimal(state.realized_cum),
            trade_count=state.trade_count,
        )

    async def save_paper(self, paper: PaperPortfolio, seed: Decimal | None = None) -> None:
        """장부 저장 — 상태 단일행 upsert + 포지션 전체 교체(포지션 수가 작아 단순함 우선)."""
        async with self._sm() as s, s.begin():
            state = await s.get(PaperStateRow, 1)
            if state is None:
                state = PaperStateRow(id=1, seed=str(seed if seed is not None else paper.cash))
                s.add(state)
            state.cash = str(paper.cash)
            state.realized_cum = str(paper.realized_cum)
            state.trade_count = paper.trade_count
            state.updated_at = datetime.now(timezone.utc)
            await s.execute(delete(PaperPositionRow))
            for symbol, pos in paper.positions.items():
                opened = pos.opened_at.astimezone(timezone.utc) if pos.opened_at else None
                s.add(PaperPositionRow(symbol=symbol, quantity=str(pos.quantity),
                                       avg_cost=str(pos.avg_cost), opened_at=opened))

    async def append_paper_equity(
        self, ts: datetime, equity: Decimal, cash: Decimal, positions_value: Decimal,
        realized_cum: Decimal, benchmark_price: Decimal | None,
    ) -> None:
        async with self._sm() as s, s.begin():
            s.add(PaperEquityRow(
                ts=ts.astimezone(timezone.utc), trade_date=trade_date_kst(ts),
                equity=str(equity), cash=str(cash), positions_value=str(positions_value),
                realized_cum=str(realized_cum),
                benchmark_price=str(benchmark_price) if benchmark_price is not None else None))

    async def count_trading_days_since(self, trade_date: str) -> int:
        """해당 KST 날짜 **이후** 틱이 돌았던 거래일 수(자산곡선 날짜 기준) — 타임스톱 입력."""
        async with self._sm() as s:
            return (await s.scalar(
                select(func.count(func.distinct(PaperEquityRow.trade_date)))
                .where(PaperEquityRow.trade_date > trade_date))) or 0

    async def load_daily_equity(self) -> list[tuple[str, Decimal, Decimal | None]]:
        """일별 자산곡선 [(날짜, 그날 마지막 equity, 그날 마지막 벤치마크가)] — 평가 입력."""
        async with self._sm() as s:
            rows = (await s.execute(
                select(PaperEquityRow).order_by(PaperEquityRow.id))).scalars().all()
        by_day: dict[str, tuple[Decimal, Decimal | None]] = {}
        for r in rows:                                        # id 오름차순 → 마지막 값이 남는다
            by_day[r.trade_date] = (
                Decimal(r.equity),
                Decimal(r.benchmark_price) if r.benchmark_price else None)
        return [(d, e, b) for d, (e, b) in sorted(by_day.items())]

    # ── 종목 유동성 통계 (ADV20 — 유니버스 2단계 선정 입력) ─────────────────────
    async def upsert_symbol_stats(self, stats: dict[str, float], trade_date: str) -> None:
        """틱이 계산한 ADV20 을 일괄 upsert(캔들에서 공짜 축적 — 추가 API 콜 없음)."""
        if not stats:
            return
        async with self._sm() as s, s.begin():
            existing = {r.symbol: r for r in (await s.execute(
                select(SymbolStatsRow).where(SymbolStatsRow.symbol.in_(stats)))).scalars()}
            for symbol, adv in stats.items():
                row = existing.get(symbol)
                if row is None:
                    s.add(SymbolStatsRow(symbol=symbol, adv20_krw=adv,
                                         updated_trade_date=trade_date))
                else:
                    row.adv20_krw, row.updated_trade_date = adv, trade_date

    async def load_adv_pool(self, size: int) -> list[str]:
        """ADV20 상위 심볼(내림차순) — 활용(exploit) 풀."""
        async with self._sm() as s:
            return list((await s.execute(
                select(SymbolStatsRow.symbol)
                .order_by(SymbolStatsRow.adv20_krw.desc()).limit(size))).scalars())

    async def load_fresh_symbols(self, cutoff_trade_date: str) -> set[str]:
        """cutoff 이후 측정된 심볼 — 나머지가 탐색(explore) 대상(미측정·낡음)."""
        async with self._sm() as s:
            return set((await s.execute(
                select(SymbolStatsRow.symbol)
                .where(SymbolStatsRow.updated_trade_date > cutoff_trade_date))).scalars())

    # ── 캔들 캐시 (429 방지 — toss/caching.CachingToss 가 사용) ─────────────────
    async def get_cached_candles(self, symbol: str, interval: str):
        """(fetched_at UTC, payload list) 또는 None. 역직렬화는 호출자(Candle 모델 의존 회피)."""
        async with self._sm() as s:
            row = (await s.execute(
                select(CandleCacheRow).where(CandleCacheRow.symbol == symbol,
                                             CandleCacheRow.interval == interval)
            )).scalars().first()
        if row is None:
            return None
        return _utc(row.fetched_at), json.loads(row.payload_json)

    async def save_cached_candles(self, symbol: str, interval: str, payload: list[dict]) -> None:
        async with self._sm() as s, s.begin():
            row = (await s.execute(
                select(CandleCacheRow).where(CandleCacheRow.symbol == symbol,
                                             CandleCacheRow.interval == interval)
            )).scalars().first()
            if row is None:
                row = CandleCacheRow(symbol=symbol, interval=interval)
                s.add(row)
            row.payload_json = json.dumps(payload, ensure_ascii=False)
            row.fetched_at = datetime.now(timezone.utc)

    # ── 조사 캐시 (§3.10 — web_search 비용 절감) ────────────────────────────────
    async def get_cached_research(self, symbol: str) -> tuple[datetime, str, list[str]] | None:
        """(fetched_at UTC, summary, sources) 또는 None."""
        async with self._sm() as s:
            row = await s.get(ResearchCacheRow, symbol)
        if row is None:
            return None
        return _utc(row.fetched_at), row.summary, json.loads(row.sources_json)

    async def save_cached_research(self, symbol: str, summary: str, sources: list[str]) -> None:
        async with self._sm() as s, s.begin():
            row = await s.get(ResearchCacheRow, symbol)
            if row is None:
                row = ResearchCacheRow(symbol=symbol)
                s.add(row)
            row.summary = summary
            row.sources_json = json.dumps(sources, ensure_ascii=False)
            row.fetched_at = datetime.now(timezone.utc)

    # ── 논문 뉴스 데이터 (§8 — 전향 수집·최초 버전 고정) ─────────────────────────
    async def insert_news(self, items: list[dict]) -> int:
        """(url, symbol) 기존 행은 건너뛰고 신규만 삽입 — 삽입 건수 반환.

        전향 수집의 최초 버전 고정: 수정 기사가 재수집돼도 같은 키면 무시된다(§8.1).
        """
        inserted = 0
        async with self._sm() as s, s.begin():
            for it in items:
                dup = (await s.execute(
                    select(NewsRow.id).where(NewsRow.url == it["url"],
                                             NewsRow.symbol == it["symbol"]).limit(1)
                )).scalar()
                if dup is None:
                    s.add(NewsRow(**it))
                    inserted += 1
        return inserted

    async def count_news(self, symbol: str | None = None) -> int:
        async with self._sm() as s:
            q = select(func.count()).select_from(NewsRow)
            if symbol:
                q = q.where(NewsRow.symbol == symbol)
            return int((await s.execute(q)).scalar() or 0)

    async def add_news_label(self, news_id: int, label: str, label_version: str) -> int:
        """골드 라벨 1건 — 재라벨링은 새 label_version 으로 append(덮어쓰기 금지·자기일치도용)."""
        async with self._sm() as s, s.begin():
            row = NewsLabelRow(news_id=news_id, label=label, label_version=label_version,
                               labeled_at=datetime.now(timezone.utc))
            s.add(row)
            await s.flush()
            return row.id

    async def add_news_model_output(self, news_id: int, *, model: str, prompt_version: str,
                                    raw_output: str, parsed_label: str | None,
                                    model_version: str | None = None) -> int:
        async with self._sm() as s, s.begin():
            row = NewsModelOutputRow(news_id=news_id, model=model, model_version=model_version,
                                     prompt_version=prompt_version, raw_output=raw_output,
                                     parsed_label=parsed_label,
                                     inferred_at=datetime.now(timezone.utc))
            s.add(row)
            await s.flush()
            return row.id

    # ── 대시보드 집계 (읽기 전용 — GUI overview) ────────────────────────────────
    async def recent_ticks(self, limit: int = 20) -> list[dict]:
        async with self._sm() as s:
            rows = (await s.execute(
                select(TickRow).order_by(TickRow.id.desc()).limit(limit))).scalars().all()
        return [{"id": r.id, "started_at": _utc(r.started_at).isoformat(), "mode": r.mode,
                 "trade_date": r.trade_date, "kill_switch": r.kill_switch,
                 "circuit_breaker": r.circuit_breaker, "circuit_breaker_reason": r.circuit_breaker_reason,
                 "universe_count": r.universe_count, "candidates": r.candidates, "note": r.note,
                 "cost_gated": json.loads(r.cost_gated_json or "[]"),
                 "regime": json.loads(r.regime_json or "{}")} for r in rows]

    async def recent_orders(self, limit: int = 20) -> list[dict]:
        async with self._sm() as s:
            rows = (await s.execute(
                select(OrderRow).order_by(OrderRow.id.desc()).limit(limit))).scalars().all()
        return [{"created_at": _utc(r.created_at).isoformat(), "symbol": r.symbol, "side": r.side,
                 "quantity": r.quantity, "price": r.price, "mode": r.mode, "status": r.status,
                 "reason": r.reason} for r in rows]

    async def recent_decisions(self, limit: int = 30) -> list[dict]:
        async with self._sm() as s:
            rows = (await s.execute(
                select(DecisionRow).order_by(DecisionRow.id.desc()).limit(limit))).scalars().all()
        return [{"symbol": r.symbol, "action": r.action, "confidence": r.confidence,
                 "rationale": r.rationale, "decision_price": r.decision_price} for r in rows]

    async def recent_audits(self, limit: int = 20) -> list[dict]:
        async with self._sm() as s:
            rows = (await s.execute(
                select(AuditRow).order_by(AuditRow.id.desc()).limit(limit))).scalars().all()
        return [{"ts": _utc(r.ts).isoformat(), "actor": r.actor, "action": r.action,
                 "payload": json.loads(r.payload_json or "{}")} for r in rows]

    async def news_stats(self, top: int = 15, recent: int = 8) -> dict:
        async with self._sm() as s:
            total = int((await s.execute(select(func.count()).select_from(NewsRow))).scalar() or 0)
            by_mapping = {m: int(c) for m, c in (await s.execute(
                select(NewsRow.mapping_method, func.count()).group_by(NewsRow.mapping_method)))}
            by_symbol = [{"symbol": sym, "count": int(c)} for sym, c in (await s.execute(
                select(NewsRow.symbol, func.count()).group_by(NewsRow.symbol)
                .order_by(func.count().desc()).limit(top)))]
            rows = (await s.execute(select(NewsRow).order_by(NewsRow.published_at.desc())
                                    .limit(recent))).scalars().all()
            latest = [{"symbol": r.symbol, "headline": r.headline, "press": r.press,
                       "published_at": _utc(r.published_at).isoformat(),
                       "mapping_method": r.mapping_method} for r in rows]
        return {"total": total, "by_mapping": by_mapping, "by_symbol": by_symbol, "recent": latest}

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

    # ── 보고서 (휴장일 자동 생성 — 기간 조회 + 중복 방지) ───────────────────────
    async def last_report_period_end(self) -> str | None:
        async with self._sm() as s:
            row = (await s.execute(
                select(ReportLogRow).order_by(ReportLogRow.id.desc()).limit(1))).scalars().first()
            return row.period_end if row else None

    async def record_report(self, period_end: str, path: str, body: str | None = None) -> None:
        async with self._sm() as s, s.begin():
            s.add(ReportLogRow(generated_at=datetime.now(timezone.utc),
                               period_end=period_end, path=path, body=body))

    async def list_reports(self, limit: int = 50) -> list[dict]:
        """보고서 목록(최신순) — 본문 제외(용량), 본문은 load_report_body."""
        async with self._sm() as s:
            rows = (await s.execute(
                select(ReportLogRow).order_by(ReportLogRow.id.desc()).limit(limit)
            )).scalars().all()
            return [{"period_end": r.period_end,
                     "generated_at": _utc(r.generated_at).isoformat(),
                     "has_body": r.body is not None} for r in rows]

    async def load_report_body(self, period_end: str) -> str | None:
        """본문(markdown) 정본 — 같은 period_end 가 여럿이면(force 재생성) 최신 것."""
        async with self._sm() as s:
            row = (await s.execute(
                select(ReportLogRow).where(ReportLogRow.period_end == period_end)
                .order_by(ReportLogRow.id.desc()).limit(1)
            )).scalars().first()
            return row.body if row else None

    async def load_period_activity(self, since_trade_date: str | None) -> dict:
        """기간(직전 보고 이후) 판단/주문/감사/틱 통계 — 보고서 렌더 입력."""
        async with self._sm() as s:
            tick_q = select(TickRow)
            dec_q = select(DecisionRow, TickRow.trade_date).join(
                TickRow, DecisionRow.tick_id == TickRow.id)
            ord_q = select(OrderRow)
            aud_q = select(AuditRow)
            if since_trade_date:
                tick_q = tick_q.where(TickRow.trade_date > since_trade_date)
                dec_q = dec_q.where(TickRow.trade_date > since_trade_date)
                ord_q = ord_q.where(OrderRow.trade_date > since_trade_date)
                since_dt = datetime.fromisoformat(since_trade_date + "T23:59:59+09:00")
                aud_q = aud_q.where(AuditRow.ts > since_dt.astimezone(timezone.utc))
            ticks = (await s.execute(tick_q)).scalars().all()
            decs = (await s.execute(dec_q)).all()
            orders = (await s.execute(ord_q)).scalars().all()
            audits = (await s.execute(aud_q)).scalars().all()
        return {
            "ticks": [{"cost_gated_json": t.cost_gated_json, "regime_json": t.regime_json}
                      for t in ticks],
            "decisions": [{"action": d.action, "symbol": d.symbol, "confidence": d.confidence,
                           "rationale": d.rationale} for d, _ in decs],
            "orders": [{"side": o.side, "symbol": o.symbol, "quantity": o.quantity,
                        "price": o.price, "status": o.status} for o in orders],
            "audits": [{"ts": str(_utc(a.ts)), "actor": a.actor, "action": a.action}
                       for a in audits],
        }

    # ── 감사 (컨트롤플레인 이벤트) ─────────────────────────────────────────────
    async def audit(self, actor: str, action: str, payload: dict | None = None) -> None:
        async with self._sm() as s, s.begin():
            s.add(AuditRow(ts=datetime.now(timezone.utc), actor=actor, action=action,
                           payload_json=json.dumps(payload or {}, ensure_ascii=False)))
