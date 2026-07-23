"""샌드박스 상시 틱 모드 — 합성 클라이언트 인터페이스 + 안전 가드(운영 DB 거부·LIVE 강등)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.main import create_app
from app.toss.sandbox import SandboxToss

KEY = {"X-API-Key": "dev-local-key"}


# ── 합성 클라이언트 ───────────────────────────────────────────────────────────
async def test_sandbox_implements_toss_interface():
    tz = SandboxToss(seed=1, day_seconds=1,
                     started_at=datetime.now(timezone.utc) - timedelta(seconds=40))
    candles = await tz.get_candles("005930")
    assert len(candles) >= 20                       # 스크리너 지표 산출에 충분한 히스토리
    c = candles[-1]
    assert c.low_price <= c.close_price <= c.high_price and c.volume > 0
    assert (await tz.get_stocks(["005930", "000660"]))[1].symbol == "000660"
    assert (await tz.get_prices(["005930"]))[0].last_price > 0
    assert (await tz.get_holdings()).items == []    # 포지션은 페이퍼 장부가 만든다
    assert (await tz.get_buying_power("KRW")).cash_buying_power == Decimal("10000000")
    assert await tz.get_stock_warnings("005930") == []
    await tz.aclose()


async def test_sandbox_price_path_is_seed_reproducible():
    a = SandboxToss(seed=7, started_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    b = SandboxToss(seed=7, started_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    c = SandboxToss(seed=8, started_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert a._path("005930", 30) == b._path("005930", 30)      # 같은 시드 → 같은 경로
    assert a._path("005930", 30) != c._path("005930", 30)      # 다른 시드 → 다른 경로


async def test_sandbox_advances_with_elapsed_time():
    """시뮬 일자는 경과 실시간으로 전진한다(봉 개수는 history 상한이라 커서로 검증)."""
    old = SandboxToss(seed=1, day_seconds=1,
                      started_at=datetime.now(timezone.utc) - timedelta(seconds=50))
    fresh = SandboxToss(seed=1, day_seconds=1)
    assert old._day() >= fresh._day() + 45              # 50초 ≈ 시뮬 50일 전진
    assert old.last_price("005930") != fresh.last_price("005930")   # 다른 시점 → 다른 가격


async def test_sandbox_starts_warmed_up():
    """첫 틱부터 스크리너가 지표를 낼 만큼의 히스토리가 있어야 후보가 나온다."""
    fresh = SandboxToss(seed=1)
    assert len(await fresh.get_candles("005930")) >= 20


# ── 안전 가드 ────────────────────────────────────────────────────────────────
def test_sandbox_refuses_production_database(monkeypatch):
    """합성 거래가 운영 페이퍼 원장·논문 데이터를 오염시키는 것을 기동 단계에서 차단."""
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host:5432/db")
    get_settings.cache_clear()
    try:
        app = create_app()
        with pytest.raises(RuntimeError, match="운영 DB"):
            with TestClient(app):
                pass
    finally:
        get_settings.cache_clear()


def test_sandbox_forces_dry_run_even_if_live_requested(monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("I_UNDERSTAND_LIVE_REAL_MONEY", "YES")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/sb.db")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            body = c.get("/api/status", headers=KEY).json()
            assert body["mode"] == "DRY_RUN"          # 합성 시세는 실계좌가 아니다
            assert body["toss_connected"] is True     # 합성 클라이언트가 주입됨
    finally:
        get_settings.cache_clear()


def test_sandbox_tick_runs_end_to_end(monkeypatch, tmp_path):
    """틱 1회가 합성 시세로 전 파이프라인을 돌고 DB 에 기록되는지(토스 호출 0)."""
    monkeypatch.setenv("SANDBOX_MODE", "true")
    monkeypatch.setenv("SANDBOX_DAY_SECONDS", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/sb2.db")
    monkeypatch.setenv("SYMBOL_SOURCE_PATH", "data/krx_symbols.json")
    monkeypatch.setenv("UNIVERSE_MAX_SYMBOLS", "5")
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "false")
    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as c:
            r = c.post("/internal/tick", headers=KEY)
            assert r.status_code == 200
            body = r.json()
            assert body["mode"] == "DRY_RUN"
            assert len(body["universe_symbols"]) >= 1   # 합성 유니버스가 실제로 돌았다
            assert all(o["status"] != "SUBMITTED" for o in body["orders"])   # 실주문 0
            over = c.get("/api/dashboard/overview", headers=KEY).json()
            assert len(over["ticks"]) == 1             # 대시보드에 기록이 보인다
    finally:
        get_settings.cache_clear()
