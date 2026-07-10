"""FastAPI 앱 골격 테스트 (TestClient).

검증: 헬스(무인증) · API키 인증 · 현황 · holdings/prices 프록시(FakeToss) · 킬스위치 토글
· 토스 미설정 시 503 · /internal/tick no-op · holdings→GuardrailContext 변환.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orders.context import context_from_holdings
from app.orders.guardrails import KST
from app.toss.models import BuyingPower, Candle, Holdings, Price, Stock

FIX = Path(__file__).parent / "fixtures"
KEY = {"X-API-Key": "dev-local-key"}
OPEN_KST = datetime(2026, 6, 23, 10, 0, tzinfo=KST)


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))["result"]


class FakeToss:
    async def get_holdings(self):
        return Holdings.model_validate(fx("holdings.json"))

    async def get_buying_power(self, currency="KRW"):
        return BuyingPower.model_validate(fx("buying_power.json"))

    async def get_prices(self, symbols):
        return [Price.model_validate(p) for p in fx("prices.json")]

    async def get_stocks(self, symbols):
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=s, market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol, interval="1d"):
        return [Candle(timestamp=f"2026-06-{1 + i:02d}T00:00:00.000+09:00",
                       open_price=1000 + i * 10, high_price=1000 + i * 10,
                       low_price=1000 + i * 10, close_price=1000 + i * 10,
                       volume=1_000_000, currency="KRW") for i in range(13)]

    async def aclose(self):   # lifespan 종료 시 호출됨
        pass


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        app.state.toss_client = FakeToss()   # lifespan은 creds 없어 None → 테스트용 주입
        yield c


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_status_requires_api_key(client):
    assert client.get("/api/status").status_code == 401
    r = client.get("/api/status", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DRY_RUN"
    assert body["kill_switch"] is False
    assert body["toss_connected"] is True
    assert "per_order_max_krw" in body["guardrails"]


def test_holdings_proxy(client):
    r = client.get("/api/holdings", headers=KEY)
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2


def test_prices_proxy(client):
    r = client.get("/api/prices", headers=KEY, params={"symbols": "005930"})
    assert r.status_code == 200
    assert r.json()[0]["symbol"] == "005930"


def test_kill_switch_toggle(client):
    r = client.post("/api/kill-switch", headers=KEY, json={"engaged": True})
    assert r.status_code == 200 and r.json()["kill_switch"] is True
    assert client.get("/api/status", headers=KEY).json()["kill_switch"] is True
    client.post("/api/kill-switch", headers=KEY, json={"engaged": False})


def test_tick_runs_dry_run(client):
    # ANTHROPIC_API_KEY 미설정 → 결정적 폴백. 워치리스트 비어있어 보유만 평가(HOLD) → 실주문 0.
    r = client.post("/internal/tick", headers=KEY)
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "DRY_RUN"
    assert "폴백" in body["engine"]
    assert body["candidates"] == 2                 # 보유 005935·AAPL
    assert isinstance(body["orders"], list)
    assert all(o["status"] != "SUBMITTED" for o in body["orders"])   # 실주문 0


def test_holdings_503_without_toss():
    app = create_app()
    with TestClient(app) as c:   # 토스 미주입 → None
        r = c.get("/api/holdings", headers=KEY)
    assert r.status_code == 503


# ── P0 §1.1: LIVE 는 DB 필수 ──────────────────────────────────────────────────
def test_live_without_db_downgrades_to_dry_run(monkeypatch):
    # DB 없는 LIVE = 일일한도 틱마다 리셋·리컨실 없음 → lifespan 이 강제 강등해야 한다
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("I_UNDERSTAND_LIVE_REAL_MONEY", "YES")
    app = create_app()
    with TestClient(app) as c:
        assert c.get("/api/status", headers=KEY).json()["mode"] == "DRY_RUN"


def test_live_with_db_stays_live(monkeypatch, tmp_path):
    from app.core.settings import get_settings

    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("I_UNDERSTAND_LIVE_REAL_MONEY", "YES")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/live.db")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            assert c.get("/api/status", headers=KEY).json()["mode"] == "LIVE"
    finally:
        get_settings.cache_clear()          # 다른 테스트가 깨끗한 설정을 읽도록


# ── P2 §3.7 하드닝 + §1.3 서킷브레이커 수동 리셋 ──────────────────────────────
def test_production_refuses_default_api_key(monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        app = create_app()
        with pytest.raises(RuntimeError, match="API_KEY"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_production_hides_docs(monkeypatch):
    from app.core.settings import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("API_KEY", "prod-strong-key")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            assert c.get("/docs").status_code == 404
            assert c.get("/openapi.json").status_code == 404
            assert c.get("/health").status_code == 200      # 헬스체크는 유지
    finally:
        get_settings.cache_clear()


def test_local_keeps_docs(client):
    assert client.get("/docs").status_code == 200


def test_circuit_breaker_reset_route(monkeypatch, tmp_path):
    from app.core.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cb.db")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            b = app.state.order_service.circuit_breaker
            b.assess(Decimal("100000"), None, OPEN_KST)     # HWM
            b.assess(Decimal("80000"), None, OPEN_KST)      # 낙폭 -20% → 래치
            assert b.tripped is True

            r = c.post("/api/circuit-breaker/reset", headers=KEY)
            assert r.status_code == 200
            assert r.json()["before"]["tripped"] is True
            assert r.json()["circuit_breaker"]["tripped"] is False
            assert b.high_water_mark is None                # 다음 틱이 새 고점 설정
    finally:
        get_settings.cache_clear()


def test_context_from_holdings():
    h = Holdings.model_validate(fx("holdings.json"))
    ctx = context_from_holdings(h, OPEN_KST)
    assert ctx.open_positions == 2
    assert {"005935", "AAPL"} <= ctx.held_symbols
    assert ctx.portfolio_value_krw == Decimal("202500")
