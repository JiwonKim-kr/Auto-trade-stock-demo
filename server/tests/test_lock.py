"""PG advisory lock(§3.4) — 다중 인스턴스 틱 직렬화.

실 PG 통합환경이 없으므로: SQLite/무DB 통과 경로는 실 엔진, PG 경로는 덕타이핑 페이크로
"같은 커넥션에서 try→unlock, 미획득 시 unlock 안 함" 계약을 고정한다.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db.lock import TICK_LOCK_KEY, pg_tick_lock
from app.db.session import make_engine
from app.main import create_app

KEY = {"X-API-Key": "dev-local-key"}


class FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val


class FakeConn:
    def __init__(self, got: bool):
        self._got = got
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params))
        return FakeResult(self._got if "pg_try_advisory_lock" in sql else None)


class FakeEngine:
    """dialect=postgresql 인 덕타이핑 엔진 — 커넥션 1개를 계속 돌려준다(동일 커넥션 계약 검증)."""

    def __init__(self, got: bool):
        self.conn = FakeConn(got)
        self.dialect = SimpleNamespace(name="postgresql")

    def connect(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False

        return _Ctx()

    async def dispose(self):   # lifespan 종료 시 호출됨
        pass


class FakeTossClient:
    async def aclose(self):    # lifespan 종료 시 호출됨
        pass


async def test_none_engine_passes_through():
    async with pg_tick_lock(None) as got:
        assert got is True


async def test_sqlite_engine_passes_through(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/l.db")
    async with pg_tick_lock(engine) as got:
        assert got is True
    await engine.dispose()


async def test_pg_acquires_and_unlocks_same_connection():
    engine = FakeEngine(got=True)
    async with pg_tick_lock(engine) as got:
        assert got is True
        assert "pg_try_advisory_lock" in engine.conn.calls[0][0]
        assert engine.conn.calls[0][1] == {"k": TICK_LOCK_KEY}
    assert "pg_advisory_unlock" in engine.conn.calls[-1][0]      # 해제는 같은 커넥션
    assert engine.conn.calls[-1][1] == {"k": TICK_LOCK_KEY}


async def test_pg_not_acquired_skips_and_never_unlocks():
    engine = FakeEngine(got=False)
    async with pg_tick_lock(engine) as got:
        assert got is False
    assert all("pg_advisory_unlock" not in sql for sql, _ in engine.conn.calls)


def test_tick_route_skips_when_other_instance_holds_lock():
    app = create_app()
    with TestClient(app) as c:
        app.state.toss_client = FakeTossClient()      # 락 스킵이 토스 접근보다 먼저
        app.state.db_engine = FakeEngine(got=False)
        r = c.post("/internal/tick", headers=KEY)
        assert r.status_code == 200
        assert "advisory" in r.json()["skipped"]
