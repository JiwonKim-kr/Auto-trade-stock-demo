"""PG advisory lock — 다중 인스턴스 틱 직렬화 (PLAN §3.4).

asyncio.Lock 은 프로세스 내부용 — Cloud Run 인스턴스가 2개 뜨면 무력하다
(`max_instance_count=1` 이 1차 방어, 이 락이 정식 해법).

함정 요약:
- advisory lock 은 **커넥션(세션)에 귀속** — 획득/해제를 같은 커넥션에서 해야 하고,
  락을 쥔 동안 그 커넥션을 풀에 반환하면 안 된다(블록 전체에서 점유 유지).
- `pg_try_advisory_lock`(비블로킹)을 쓴다 — 이미 도는 틱이 있으면 대기 없이 스킵
  (in-process asyncio 락과 같은 시맨틱).
- 커넥션이 끊기면 PG 가 자동 해제(크래시 안전 — 고아 락 없음).
- Supabase 는 세션 모드 풀러(5432)에서만 유효 — 트랜잭션 모드(6543)는 문장마다
  커넥션이 바뀌어 무의미(§3.0 DB 결정에서 세션 모드 강제).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

TICK_LOCK_KEY = 0x544F5353   # 임의 고정 상수("TOSS") — 앱 전역에서 이 키 하나만 사용


@asynccontextmanager
async def pg_tick_lock(engine: AsyncEngine | None) -> AsyncIterator[bool]:
    """True = 락 획득(또는 PG 아님 — 로컬은 asyncio 락이 이미 직렬화). False = 스킵."""
    if engine is None or engine.dialect.name != "postgresql":
        yield True
        return
    async with engine.connect() as conn:          # 풀에서 1개 점유 — 블록 끝까지 유지
        got = (await conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": TICK_LOCK_KEY})).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK_KEY})
