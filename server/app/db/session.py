"""DB 엔진/세션 팩토리 + 스키마 생성.

마이그레이션 결정: 현재는 시작 시 `create_all` + 추가 컬럼 목록(아래) — `create_all` 은
**기존 테이블을 변경하지 않으므로** 이미 만들어진 로컬 DB 에 컬럼이 추가되면 여기 등록한다
(ALTER ADD COLUMN 은 SQLite/PG 공통·무손실). 스키마 진화가 본격화되면 Alembic 도입 — TECH-STACK §4.
URL 예: 운영 `postgresql+asyncpg://...`, 로컬 `sqlite+aiosqlite:///./trading.db`.
"""

from __future__ import annotations

import re

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
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")     # DDL 에 문자열로 들어가므로 화이트리스트


def _valid_schema(schema: str) -> str:
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"허용되지 않는 스키마 이름: {schema!r}")
    return schema


def make_engine(database_url: str, schema: str | None = None) -> AsyncEngine:
    """schema 지정 시(PG 전용) 해당 스키마로 search_path 를 고정 — 같은 DB 안에서 완전 분리.

    샌드박스(합성 거래)가 운영 테이블을 건드리지 않게 하는 **구조적** 격리 수단이다
    (권한이 아니라 경로 분리 — public 을 search_path 에서 빼므로 운영 테이블이 보이지 않는다).
    SQLite 는 스키마 개념이 없어 무시된다(로컬 샌드박스는 별도 파일로 분리).
    """
    kwargs: dict = {}
    if schema and database_url.startswith("postgresql"):
        kwargs["connect_args"] = {"server_settings": {"search_path": _valid_schema(schema)}}
    return create_async_engine(database_url, pool_pre_ping=True, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


def _apply_additive_migrations(conn) -> None:
    insp = inspect(conn)
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if insp.has_table(table):
            existing = {c["name"] for c in insp.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


async def init_db(engine: AsyncEngine, schema: str | None = None) -> None:
    """테이블 생성(존재하면 no-op) + 등록된 추가 컬럼 반영. schema 지정 시 먼저 스키마를 만든다."""
    async with engine.begin() as conn:
        if schema and engine.dialect.name == "postgresql":
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_valid_schema(schema)}"'))
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_additive_migrations)
