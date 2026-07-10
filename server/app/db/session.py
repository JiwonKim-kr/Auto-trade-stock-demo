"""DB 엔진/세션 팩토리 + 스키마 생성.

마이그레이션 결정: 현재는 시작 시 `create_all` + 추가 컬럼 목록(아래) — `create_all` 은
**기존 테이블을 변경하지 않으므로** 이미 만들어진 로컬 DB 에 컬럼이 추가되면 여기 등록한다
(ALTER ADD COLUMN 은 SQLite/PG 공통·무손실). 스키마 진화가 본격화되면 Alembic 도입 — TECH-STACK §4.
URL 예: 운영 `postgresql+asyncpg://...`, 로컬 `sqlite+aiosqlite:///./trading.db`.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base

# (테이블, 컬럼, DDL 타입) — nullable 만 허용(기존 행 호환). §3.9 body 가 첫 사례.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("report_log", "body", "TEXT"),
]


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def _apply_additive_migrations(conn) -> None:
    insp = inspect(conn)
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if insp.has_table(table):
            existing = {c["name"] for c in insp.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


async def init_db(engine: AsyncEngine) -> None:
    """스키마 생성(존재하면 no-op) + 등록된 추가 컬럼 반영."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_additive_migrations)
