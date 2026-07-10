"""P1 테스트 — 캔들 TTL 캐시(§2.1) + 알림/억제 게이트(§3.5)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient

from app.core.notify import AlertGate, TelegramNotifier
from app.core.settings import Settings
from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.main import create_app
from app.orders.reconcile import PositionSnapshot
from app.toss.caching import CachingToss
from app.toss.models import BuyingPower, Candle, Holdings, Stock

FIX = Path(__file__).parent / "fixtures"
KEY = {"X-API-Key": "dev-local-key"}


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


def _candles(n=3) -> list[Candle]:
    return [Candle(timestamp=f"2026-07-{1 + i:02d}T00:00:00.000+09:00",
                   open_price=1000, high_price=1010, low_price=990, close_price=1000 + i,
                   volume=1_000, currency="KRW") for i in range(n)]


class CountingInner:
    def __init__(self):
        self.calls = 0

    async def get_candles(self, symbol, interval="1d"):
        self.calls += 1
        return _candles()

    async def get_holdings(self):
        return "delegated"                                     # __getattr__ 위임 검증용


# ── 캔들 캐시 ─────────────────────────────────────────────────────────────────
async def test_cache_hit_skips_inner(tmp_path):
    repo = await make_repo(tmp_path)
    inner = CountingInner()
    caching = CachingToss(inner, repo, ttl_minutes=60)
    first = await caching.get_candles("005930")
    second = await caching.get_candles("005930")
    assert inner.calls == 1                                    # TTL 내 재호출 = 캐시 히트
    assert second == first and second[0].close_price == Decimal("1000")   # 왕복 무손실


async def test_cache_expiry_refetches(tmp_path):
    repo = await make_repo(tmp_path)
    inner = CountingInner()
    caching = CachingToss(inner, repo, ttl_minutes=0)          # TTL 0 = 항상 만료
    await caching.get_candles("005930")
    await caching.get_candles("005930")
    assert inner.calls == 2


async def test_cache_delegates_other_methods(tmp_path):
    repo = await make_repo(tmp_path)
    caching = CachingToss(CountingInner(), repo, ttl_minutes=60)
    assert await caching.get_holdings() == "delegated"         # get_candles 외엔 위임


# ── AlertGate (반복 알림 억제) ─────────────────────────────────────────────────
def test_alert_gate_suppresses_within_window():
    gate = AlertGate()
    assert gate.allow("k") is True
    assert gate.allow("k") is False                            # 60분 내 동일 키 억제
    assert gate.allow("other") is True                         # 다른 키는 허용
    gate._last_sent["k"] -= 7200                               # 창 경과 시뮬레이션
    assert gate.allow("k") is True


# ── TelegramNotifier (실패 무해성) ────────────────────────────────────────────
async def test_telegram_failure_is_swallowed():
    def handler(request):
        raise httpx.ConnectError("down")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TelegramNotifier("tok", "chat", http=http)
    await n.send("hello")                                      # 예외가 새어나오면 실패


async def test_telegram_posts_chat_and_text():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await TelegramNotifier("tok", "chat-1", http=http).send("본문")
    assert seen == {"chat_id": "chat-1", "text": "본문"}


# ── 라우트 통합: 전이 알림·중복 억제 ──────────────────────────────────────────
class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class LossToss:
    """일일 손실 -6% 보유 — 서킷브레이커 발동 시나리오(페이퍼 off 전제)."""

    async def get_holdings(self):
        return Holdings.model_validate({
            "totalPurchaseAmount": {"krw": "229000"},
            "marketValue": {"amount": {"krw": "202500"}},
            "profitLoss": {"amount": {"krw": "-26500"}, "rate": "-0.1155"},
            "dailyProfitLoss": {"amount": {"krw": "-70000"}, "rate": "-0.06"},
            "items": [{"symbol": "005930", "name": "삼성전자", "currency": "KRW", "quantity": "1",
                       "lastPrice": "202500", "averagePurchasePrice": "229000",
                       "marketValue": {"purchaseAmount": "229000", "amount": "202500"},
                       "profitLoss": {"amount": "-26500", "rate": "-0.1157"}}],
        })

    async def get_stocks(self, symbols):
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        return _candles(13)

    async def get_buying_power(self, currency="KRW"):
        return BuyingPower(currency="KRW", cash_buying_power=Decimal("1000000"))

    async def aclose(self):
        pass


async def test_circuit_breaker_transition_notifies_once(tmp_path):
    repo = await make_repo(tmp_path)
    fake = FakeNotifier()
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.toss_client = LossToss()
        app.state.repo = repo
        app.state.notifier = fake
        app.state.settings = Settings(paper_seed_krw=Decimal("0"))   # 실보유 경로(일일손익 전달)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/internal/tick", headers=KEY)              # 발동(전이) → 알림 1
            await c.post("/internal/tick", headers=KEY)              # 유지(전이 없음) → 무음
    cb_msgs = [m for m in fake.messages if "서킷브레이커" in m]
    assert len(cb_msgs) == 1 and "발동" in cb_msgs[0]


async def test_reconcile_mismatch_notify_deduped(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.save_positions_snapshot(
        datetime.now().astimezone(), [PositionSnapshot(symbol="005930", quantity=Decimal("999"))])
    fake = FakeNotifier()
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.toss_client = LossToss()                     # 실보유 1주 ≠ 기준선 999
        app.state.repo = repo
        app.state.notifier = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get("/api/reconcile", headers=KEY)         # 기준선 미이동 → 같은 불일치
            await c.get("/api/reconcile", headers=KEY)
    mismatch = [m for m in fake.messages if "리컨실 불일치" in m]
    assert len(mismatch) == 1                                  # AlertGate 60분 억제


async def test_kill_switch_toggle_notifies():
    fake = FakeNotifier()
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.notifier = fake
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/api/kill-switch", headers=KEY, json={"engaged": True})
    assert any("킬스위치 ON" in m for m in fake.messages)
