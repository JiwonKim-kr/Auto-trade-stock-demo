"""휴장일 캘린더(§3.6) + 자동 보고서(§7.2) 테스트."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.api.report import generate_report, maybe_generate_report, scheduled_report
from app.core.calendar import is_trading_day, load_holidays
from app.core.settings import Settings
from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.engine.evaluation import evaluate
from app.engine.report import render_report, summary_line
from app.main import create_app
from app.orders.guardrails import KST, GuardrailConfig
from app.orders.models import OrderRequest, OrderType, Side
from app.orders.service import OrderService

KEY = {"X-API-Key": "dev-local-key"}


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


# ── 캘린더 ────────────────────────────────────────────────────────────────────
def test_calendar_weekend_and_holiday():
    holidays = frozenset({"2026-01-01"})
    assert is_trading_day(date(2026, 7, 4), holidays) is False    # 토
    assert is_trading_day(date(2026, 1, 1), holidays) is False    # 공휴일(목)
    assert is_trading_day(date(2026, 7, 2), holidays) is True     # 평일


def test_calendar_loads_bundled_file():
    holidays = load_holidays()                                    # data/krx_holidays.json
    assert "2026-01-01" in holidays and "2026-05-05" in holidays


def test_calendar_missing_file_falls_back(tmp_path):
    assert load_holidays(tmp_path / "nope.json") == frozenset()


def test_guardrail_blocks_holiday_order():
    svc = OrderService(config=GuardrailConfig(holidays=frozenset({"2026-01-01"})))
    from app.orders.guardrails import GuardrailContext
    o = OrderRequest(symbol="005930", side=Side.BUY, order_type=OrderType.LIMIT,
                     quantity=Decimal("1"), price=Decimal("1000"))
    res = svc.submit(o, GuardrailContext(now=datetime(2026, 1, 1, 10, 0, tzinfo=KST)))
    assert "공휴일" in res.reason                                  # 목요일이지만 휴장


# ── 렌더러(순수) ─────────────────────────────────────────────────────────────
def _equity_rows():
    return [("2026-07-01", Decimal("10000000"), Decimal("200")),
            ("2026-07-02", Decimal("10100000"), Decimal("202"))]


def test_render_report_sections():
    er = evaluate([(d, e) for d, e, _ in _equity_rows()], n_trades=3)
    text = render_report(
        period_start=None, period_end="2026-07-02", equity_rows=_equity_rows(),
        eval_report=er,
        decisions=[{"action": "SELL", "symbol": "005930", "confidence": 1.0,
                    "rationale": "결정적 청산: 손절 -9.0% ≤ -8.0%"}],
        orders=[{"side": "SELL", "symbol": "005930", "quantity": "10", "price": "9000",
                 "status": "DRY_RUN"}],
        audits=[{"ts": "2026-07-02", "actor": "system", "action": "reconcile_mismatch"}],
        ticks=[{"cost_gated_json": '["035720"]', "regime_json": '{"level": "CALM"}'}])
    for needle in ("페이퍼 운용 보고서", "누적 평가", "강제 청산", "비용 게이트 차단 매수 후보: 1건",
                   "CALM", "reconcile_mismatch"):
        assert needle in text
    assert "N=3" in summary_line("2026-07-02", er, _equity_rows())


# ── 생성/트리거 (경계) ────────────────────────────────────────────────────────
async def _seed_equity(repo):
    for i, eq in enumerate(("10000000", "10100000")):
        await repo.append_paper_equity(datetime(2026, 7, 1 + i, 15, 0, tzinfo=KST),
                                       Decimal(eq), Decimal(eq), Decimal(0), Decimal(0), None)


async def test_report_route_writes_file_and_dedupes(tmp_path):
    repo = await make_repo(tmp_path)
    await _seed_equity(repo)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        app.state.settings = Settings(reports_dir=str(tmp_path / "reports"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/internal/report", headers=KEY, params={"force": "true"})
        body = r.json()
        assert Path(body["path"]).is_file() and body["period_end"] == "2026-07-02"
        again = await generate_report(app, force=False)            # 새 데이터 없음 → 스킵
        assert "skipped" in again


async def test_maybe_generate_report_only_on_non_trading_day(tmp_path):
    repo = await make_repo(tmp_path)
    await _seed_equity(repo)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        app.state.settings = Settings(reports_dir=str(tmp_path / "reports"))
        await maybe_generate_report(app, datetime(2026, 7, 3, 10, 0, tzinfo=KST))   # 금(거래일)
        assert await repo.last_report_period_end() is None                          # 미생성
        await maybe_generate_report(app, datetime(2026, 7, 4, 10, 0, tzinfo=KST))   # 토(휴장)
        assert await repo.last_report_period_end() == "2026-07-02"                  # 생성됨


async def test_report_skips_without_data(tmp_path):
    repo = await make_repo(tmp_path)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        result = await generate_report(app, force=True)
    assert "skipped" in result


# ── §3.9 클라우드 영속 — 본문 DB 정본 · best-effort 파일 · maybe 라우트 ──────────
async def test_report_body_persisted_even_if_file_write_fails(tmp_path):
    repo = await make_repo(tmp_path)
    await _seed_equity(repo)
    blocker = tmp_path / "blocked"
    blocker.write_text("파일 — reports_dir 자리를 차지해 mkdir 실패 유도", encoding="utf-8")
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        app.state.settings = Settings(reports_dir=str(blocker))    # mkdir → OSError
        result = await generate_report(app, force=True)
        assert result["period_end"] == "2026-07-02"
        assert result["path"] is None                              # 파일은 실패, 생성은 성공
        body = await repo.load_report_body("2026-07-02")
        assert body and "페이퍼 운용 보고서" in body                # DB 정본


async def test_scheduled_report_skips_trading_day(tmp_path):
    repo = await make_repo(tmp_path)
    await _seed_equity(repo)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        app.state.settings = Settings(reports_dir=str(tmp_path / "reports"))
        r1 = await scheduled_report(app, datetime(2026, 7, 3, 10, 0, tzinfo=KST))   # 금(거래일)
        assert "거래일" in r1["skipped"]
        r2 = await scheduled_report(app, datetime(2026, 7, 4, 10, 0, tzinfo=KST))   # 토(휴장)
        assert r2["period_end"] == "2026-07-02"


async def test_reports_api_list_and_body(tmp_path):
    repo = await make_repo(tmp_path)
    await _seed_equity(repo)
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.repo = repo
        app.state.settings = Settings(reports_dir=str(tmp_path / "reports"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/internal/report", headers=KEY, params={"force": "true"})
            lst = (await c.get("/api/reports", headers=KEY)).json()
            assert lst[0]["period_end"] == "2026-07-02" and lst[0]["has_body"] is True
            r = await c.get("/api/reports/2026-07-02", headers=KEY)
            assert r.status_code == 200 and "페이퍼 운용 보고서" in r.text
            assert (await c.get("/api/reports/1999-01-01", headers=KEY)).status_code == 404


async def test_init_db_migrates_legacy_report_log(tmp_path):
    """create_all 은 기존 테이블을 안 바꾼다 — body 없는 구버전 DB 에 컬럼이 추가되는지."""
    from sqlalchemy import text as sql_text

    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy.db")
    async with engine.begin() as conn:
        await conn.execute(sql_text(
            "CREATE TABLE report_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "generated_at DATETIME, period_end TEXT, path TEXT)"))
    await init_db(engine)                                          # 추가 컬럼 마이그레이션
    repo = Repository(make_sessionmaker(engine))
    await repo.record_report("2026-07-02", "", body="본문")
    assert await repo.load_report_body("2026-07-02") == "본문"
