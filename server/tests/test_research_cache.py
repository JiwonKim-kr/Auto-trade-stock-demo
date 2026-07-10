"""조사 캐시(§3.10) — 심볼당 TTL 재사용으로 web_search(비용 지배 항목) 절감."""

from __future__ import annotations

from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.engine.llm import CandidateContext, ResearchNote
from app.engine.research import CachingResearch


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/r.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


def ctx(symbol: str = "005930", held: bool = False) -> CandidateContext:
    return CandidateContext(symbol=symbol, name="삼성전자", market="KOSPI", currency="KRW",
                            indicators=None, score=0.0, already_held=held)


class CountingResearch:
    def __init__(self, summary_prefix: str = "실조사"):
        self.calls = 0
        self._prefix = summary_prefix

    async def research(self, c: CandidateContext) -> ResearchNote:
        self.calls += 1
        return ResearchNote(symbol=c.symbol, summary=f"{self._prefix} {self.calls}",
                            sources=["https://example.com/news"])


async def test_cache_hit_skips_inner_and_marks_freshness(tmp_path):
    inner = CountingResearch()
    caching = CachingResearch(inner, await make_repo(tmp_path))
    first = await caching.research(ctx())
    assert inner.calls == 1 and first.summary == "실조사 1"     # 첫 콜은 실조사(표기 없음)
    second = await caching.research(ctx())
    assert inner.calls == 1                                     # TTL 내 → 실조사 안 함
    assert "캐시된 조사" in second.summary and "실조사 1" in second.summary
    assert second.sources == ["https://example.com/news"]


async def test_ttl_zero_always_refetches(tmp_path):
    inner = CountingResearch()
    caching = CachingResearch(inner, await make_repo(tmp_path), ttl_minutes=0)
    await caching.research(ctx())
    await caching.research(ctx())
    assert inner.calls == 2


async def test_held_symbol_uses_short_ttl(tmp_path):
    inner = CountingResearch()
    caching = CachingResearch(inner, await make_repo(tmp_path),
                              ttl_minutes=1440, held_ttl_minutes=0)
    await caching.research(ctx(held=True))
    await caching.research(ctx(held=True))      # 보유 TTL(0) 경과 → 재조사
    assert inner.calls == 2
    await caching.research(ctx(held=False))     # 비보유는 일반 TTL → 캐시
    assert inner.calls == 2


async def test_empty_note_not_cached(tmp_path):
    class EmptyResearch:
        def __init__(self):
            self.calls = 0

        async def research(self, c):
            self.calls += 1
            return ResearchNote(symbol=c.symbol, summary="", sources=[])

    inner = EmptyResearch()
    repo = await make_repo(tmp_path)
    caching = CachingResearch(inner, repo)
    await caching.research(ctx())
    await caching.research(ctx())               # 빈 노트는 미캐시 → 매번 재시도
    assert inner.calls == 2
    assert await repo.get_cached_research("005930") is None


async def test_different_symbols_cached_separately(tmp_path):
    inner = CountingResearch()
    caching = CachingResearch(inner, await make_repo(tmp_path))
    await caching.research(ctx("005930"))
    await caching.research(ctx("000660"))
    assert inner.calls == 2
    await caching.research(ctx("005930"))
    await caching.research(ctx("000660"))
    assert inner.calls == 2
