"""GUI 대시보드(overview API + 셸 페이지) 테스트."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.main import create_app
from app.orders.guardrails import KST

KEY = {"X-API-Key": "dev-local-key"}


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/d.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


def _news_row(sym="005930", url="https://n/1", mapping="naver_query+title_match"):
    now = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)
    return {"symbol": sym, "headline": "삼성전자 2분기 실적 발표", "press": "news.example",
            "url": url, "published_at": now, "collected_at": now,
            "source": "naver_api_hub", "mapping_method": mapping}


async def test_dashboard_page_served():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/dashboard")
    assert r.status_code == 200
    assert "대시보드" in r.text and "overview" in r.text     # 셸 무인증 · JS 가 데이터 폴링


async def test_overview_requires_api_key():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/dashboard/overview")).status_code == 401


async def test_overview_without_db_status_only():
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            body = (await c.get("/api/dashboard/overview", headers=KEY)).json()
    assert body["persistence"] is False
    assert body["status"]["mode"] == "DRY_RUN"
    assert "circuit_breaker" in body["status"]


async def test_overview_with_data(tmp_path):
    repo = await make_repo(tmp_path)
    for i, eq in enumerate(("10000000", "10120000", "10080000")):     # 3일 → 평가 지표 산출
        # (ts, equity, cash, positions_value, realized_cum, benchmark_price)
        await repo.append_paper_equity(datetime(2026, 7, 1 + i, 15, 0, tzinfo=KST),
                                       Decimal(eq), Decimal("5000000"), Decimal("5000000"),
                                       Decimal("0"), Decimal("35000") + i * 100)
    await repo.insert_news([_news_row(), _news_row(url="https://n/2",
                                                   mapping="naver_query+desc_match")])
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            body = (await c.get("/api/dashboard/overview", headers=KEY)).json()

    assert body["persistence"] is True
    assert len(body["equity"]) == 3
    assert body["equity"][0]["benchmark"] is not None
    assert body["evaluation"]["cumulative_return"] is not None      # 3일 → 산출됨
    assert body["news"]["total"] == 2
    assert body["news"]["by_mapping"]["naver_query+title_match"] == 1
    assert body["news"]["by_mapping"]["naver_query+desc_match"] == 1
    assert isinstance(body["ticks"], list) and isinstance(body["audits"], list)
