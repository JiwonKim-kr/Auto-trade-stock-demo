"""DB 엔진/세션 팩토리 + 스키마 생성.

마이그레이션 결정: 현재는 시작 시 `create_all`(스키마가 아직 초기·단일 서비스라 충분).
스키마 진화가 시작되면 Alembic 도입(pyproject future 그룹) — TECH-STACK §4.
URL 예: 운영 `postgresql+asyncpg://...`(Cloud SQL), 로컬 `sqlite+aiosqlite:///./trading.db`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """스키마 생성(존재하면 no-op)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
