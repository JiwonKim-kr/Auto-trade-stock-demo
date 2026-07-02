"""DB 영속화 테스트 — repo 단위(SQLite/aiosqlite) + 라우트 통합(ASGI).

핵심 검증: 틱/결정/주문 기록 · clientOrderId UNIQUE 멱등 · 일일 매수 사용액 합산(교차-틱 한도 근거)
· 엔진 상태(킬스위치·서킷브레이커) 왕복 · 라우트 배선(tick_id, kill-switch 영속+감사).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db.models import AuditRow, DecisionRow, OrderRow, TickRow
from app.db.repo import Repository, trade_date_kst
from app.db.session import init_db, make_engine, make_sessionmaker
from app.engine.llm import Action, Decision
from app.engine.pipeline import TickResult
from app.main import create_app
from app.orders.guardrails import KST
from app.orders.models import (
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    TradingMode,
)
from app.toss.models import BuyingPower, Candle, Holdings, Price, Stock

FIX = Path(__file__).parent / "fixtures"
KEY = {"X-API-Key": "dev-local-key"}
NOW_KST = datetime(2026, 7, 2, 10, 0, tzinfo=KST)


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


def order_result(client_order_id: str, side=Side.BUY, status=OrderStatus.DRY_RUN,
                 qty="2", price="10000", created_at=None) -> OrderResult:
    req = OrderRequest(client_order_id=client_order_id, symbol="005930", side=side,
                       order_type=OrderType.LIMIT, quantity=Decimal(qty), price=Decimal(price))
    return OrderResult(client_order_id=client_order_id, status=status, mode=TradingMode.DRY_RUN,
                       request=req, created_at=created_at or NOW_KST.astimezone(timezone.utc))


def tick_result(orders, decisions=None) -> TickResult:
    return TickResult(mode="DRY_RUN", kill_switch=False, universe_symbols=["005930"],
                      candidates=1, decisions=decisions or [], orders=orders,
                      cost_gated=["035720"], regime={"level": "ELEVATED", "multiplier": "0.5"})


# ── repo 단위 ─────────────────────────────────────────────────────────────────
async def test_record_tick_persists_all(tmp_path):
    repo = await make_repo(tmp_path)
    decisions = [Decision(action=Action.BUY, symbol="005930", confidence=0.8, rationale="근거")]
    tick_id = await repo.record_tick(tick_result([order_result("o-1")], decisions), NOW_KST)

    async with repo._sm() as s:
        tick = await s.get(TickRow, tick_id)
        d = (await s.execute(select(DecisionRow))).scalars().one()
        o = (await s.execute(select(OrderRow))).scalars().one()
    assert tick.candidates == 1 and json.loads(tick.cost_gated_json) == ["035720"]
    assert json.loads(tick.regime_json)["level"] == "ELEVATED"        # 사이징 축소 근거 감사
    assert d.tick_id == tick_id and d.action == "BUY" and d.rationale == "근거"
    assert o.client_order_id == "o-1" and o.quantity == "2" and o.price == "10000"
    assert o.trade_date == "2026-07-02"                      # KST 날짜


async def test_duplicate_client_order_id_recorded_once(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.record_tick(tick_result([order_result("dup-1")]), NOW_KST)
    await repo.record_tick(tick_result([order_result("dup-1")]), NOW_KST)   # 재기록 시도
    dup_echo = order_result("dup-1", status=OrderStatus.DUPLICATE)
    await repo.record_tick(tick_result([dup_echo]), NOW_KST)                # 멱등 에코
    async with repo._sm() as s:
        rows = (await s.execute(select(OrderRow))).scalars().all()
    assert len(rows) == 1                                     # UNIQUE 멱등 2차 방어


async def test_buy_notional_today_sums_intended_buys_only(tmp_path):
    repo = await make_repo(tmp_path)
    yesterday = datetime(2026, 7, 1, 10, 0, tzinfo=KST).astimezone(timezone.utc)
    orders = [
        order_result("b1", qty="2", price="10000"),                          # 오늘 BUY 20000
        order_result("b2", qty="1", price="30000"),                          # 오늘 BUY 30000
        order_result("r1", status=OrderStatus.REJECTED, qty="9", price="9999"),   # 거부 → 제외
        order_result("s1", side=Side.SELL, qty="5", price="10000"),          # 매도 → 제외
        order_result("y1", qty="4", price="10000", created_at=yesterday),    # 어제 → 제외
    ]
    await repo.record_tick(tick_result(orders), NOW_KST)
    used = await repo.buy_notional_today(trade_date_kst(NOW_KST))
    assert used == Decimal("50000")


async def test_engine_state_roundtrip(tmp_path):
    repo = await make_repo(tmp_path)
    assert await repo.load_engine_state() is None             # 초기 없음
    breaker = {"high_water_mark": "1000000", "drawdown_halt": True}
    await repo.save_engine_state(True, breaker)
    ks, restored = await repo.load_engine_state()
    assert ks is True and restored == breaker
    await repo.save_engine_state(False, {})                   # 단일행 갱신(중복행 없음)
    ks2, _ = await repo.load_engine_state()
    assert ks2 is False


async def test_audit_row(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.audit("api", "kill_switch", {"engaged": True})
    async with repo._sm() as s:
        row = (await s.execute(select(AuditRow))).scalars().one()
    assert row.actor == "api" and json.loads(row.payload_json)["engaged"] is True


# ── 라우트 통합 (ASGI + lifespan 수동 구동) ───────────────────────────────────
class FakeToss:
    async def get_holdings(self):
        data = json.loads((FIX / "holdings.json").read_text(encoding="utf-8"))["result"]
        return Holdings.model_validate(data)

    async def get_buying_power(self, currency="KRW"):
        data = json.loads((FIX / "buying_power.json").read_text(encoding="utf-8"))["result"]
        return BuyingPower.model_validate(data)

    async def get_prices(self, symbols):
        return [Price.model_validate(p) for p in
                json.loads((FIX / "prices.json").read_text(encoding="utf-8"))["result"]]

    async def get_stocks(self, symbols):
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        return [Candle(timestamp=f"2026-06-{1 + i:02d}T00:00:00.000+09:00",
                       open_price=1000 + i * 10, high_price=1000 + i * 10,
                       low_price=1000 + i * 10, close_price=1000 + i * 10,
                       volume=1_000_000, currency="KRW") for i in range(13)]

    async def aclose(self):
        pass


async def test_tick_route_records_and_saves_state(tmp_path):
    repo = await make_repo(tmp_path)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.toss_client = FakeToss()
        app.state.repo = repo
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/internal/tick", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["tick_id"] == 1                               # 기록됨
    async with repo._sm() as s:
        assert (await s.get(TickRow, 1)) is not None
        n_dec = len((await s.execute(select(DecisionRow))).scalars().all())
    assert n_dec == body["candidates"] > 0                    # 결정 전수 로깅
    assert await repo.load_engine_state() is not None         # 엔진 상태 저장됨


async def test_kill_switch_route_persists_and_audits(tmp_path):
    repo = await make_repo(tmp_path)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/kill-switch", headers=KEY, json={"engaged": True})
    assert r.json()["kill_switch"] is True
    ks, _ = await repo.load_engine_state()
    assert ks is True                                         # 재시작 생존
    async with repo._sm() as s:
        audit = (await s.execute(select(AuditRow))).scalars().one()
    assert audit.action == "kill_switch"
