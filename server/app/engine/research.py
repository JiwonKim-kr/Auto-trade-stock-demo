"""조사 단계 — 후보(매수+보유)별 컨텍스트 확보. 기본: Claude + web_search(최신 사실).

결정 단계 앞에 둔다. 후보별 `ResearchNote(summary, sources)`를 만들어 CandidateContext.research에
붙이면, 결정 프롬프트의 [조사] 섹션으로 들어간다. 낡은 기억 대신 **방금 검색한 grounded 데이터**라
환각을 줄인다(결정 단계는 여전히 자기 기억은 불신).

비용 주의: web_search는 후보당 검색이라 비용·지연이 든다 → top_n으로 상위 후보만 조사하는 것을 권장.
조사 모델은 결정 모델(Fable 5)과 분리(기본 claude-opus-4-8) — 검색·요약엔 충분하고 더 저렴.
클라이언트 주입형이라 API 키 없이 테스트 가능.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.engine.llm import CandidateContext, ResearchNote

_KST = timezone(timedelta(hours=9))

RESEARCH_SYSTEM = """당신은 한국 주식 리서치 보조자다. 주어진 종목에 대해 웹에서 최신 정보를 검색해
(최근 뉴스·공시·실적·이벤트·업종 동향) 매매 판단에 쓸 **사실 위주의 간결한 한국어 브리프**를 작성한다.
- 검색으로 확인된 사실만. 추측·과장 금지. 가격 예측이나 매수/매도 추천은 하지 않는다(다른 단계가 한다).
- 3~5문장. 날짜가 있으면 명시. 특이사항이 확인되지 않으면 "특이사항 없음"."""


@dataclass(frozen=True)
class ResearchConfig:
    model: str = "claude-opus-4-8"   # 조사용(결정 모델과 분리). 검색+요약엔 충분
    max_tokens: int = 2000
    max_searches: int = 4            # web_search max_uses (비용 상한)
    max_continuations: int = 3       # pause_turn(서버툴 루프) 연속 횟수 상한


class ResearchProvider(Protocol):
    async def research(self, ctx: CandidateContext) -> ResearchNote: ...


class NullResearch:
    """조사 비활성 — 빈 노트."""

    async def research(self, ctx: CandidateContext) -> ResearchNote:
        return ResearchNote(symbol=ctx.symbol, summary="", sources=[])


def _research_query(ctx: CandidateContext) -> str:
    held = "보유 중" if ctx.already_held else "신규 매수 후보"
    return (f"{ctx.symbol} {ctx.name} ({ctx.market or 'KR'}) — {held}. "
            "최근 뉴스·공시·실적·이벤트를 조사해 매매 판단용 사실 브리프를 작성하라.")


def _collect_text(resp) -> str:
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text" and b.text]
    return "\n".join(parts).strip()


def _collect_sources(resp) -> list[str]:
    """web_search 결과/인용에서 URL 추출(best-effort). 응답 형태가 다양해 안전하게."""
    urls: list[str] = []
    for block in resp.content:
        url = getattr(block, "url", None)
        if isinstance(url, str):
            urls.append(url)
        for cite in getattr(block, "citations", None) or []:
            cu = getattr(cite, "url", None)
            if isinstance(cu, str):
                urls.append(cu)
    return list(dict.fromkeys(urls))  # 중복 제거, 순서 보존


class WebSearchResearch:
    """Claude + web_search 기반 조사기. 클라이언트 주입형."""

    def __init__(self, client: Any | None = None, config: ResearchConfig | None = None):
        self.config = config or ResearchConfig()
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def research(self, ctx: CandidateContext) -> ResearchNote:
        client = self._ensure_client()
        tools = [{"type": "web_search_20260209", "name": "web_search",
                  "max_uses": self.config.max_searches}]
        messages: list[dict] = [{"role": "user", "content": _research_query(ctx)}]

        async def _call():
            return await client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=RESEARCH_SYSTEM,
                tools=tools,
                messages=messages,
            )

        resp = await _call()
        cont = 0
        # 서버툴(web_search) 루프 한도 → pause_turn 이면 이어서 재요청
        while getattr(resp, "stop_reason", None) == "pause_turn" and cont < self.config.max_continuations:
            messages.append({"role": "assistant", "content": resp.content})
            resp = await _call()
            cont += 1

        summary = _collect_text(resp) or "특이사항 없음"
        return ResearchNote(symbol=ctx.symbol, summary=summary, sources=_collect_sources(resp))


class CachingResearch:
    """provider 래퍼 — 심볼당 TTL 캐시(DB, §3.10). 조립은 경계(tick.py — §0-6).

    web_search 는 콜당 검색 비용 + 결과 토큰이 커서 LLM 비용의 지배 항목인데, 일봉 전략에서
    같은 심볼을 하루 여러 번 조사할 정보 가치는 낮다. 보유 종목은 매도 판단에 뉴스 신선도가
    중요하므로 별도의 짧은 TTL. 캐시 반환 시 수집 시각을 브리프 앞에 표기해
    판단 LLM 이 신선도를 알게 한다. 빈 노트(조사 실패/비활성)는 캐시하지 않는다.
    """

    def __init__(self, inner: ResearchProvider, repo, *,
                 ttl_minutes: int = 1440, held_ttl_minutes: int = 120):
        self._inner = inner
        self._repo = repo
        self._ttl = timedelta(minutes=ttl_minutes)
        self._held_ttl = timedelta(minutes=held_ttl_minutes)

    async def research(self, ctx: CandidateContext) -> ResearchNote:
        cached = await self._repo.get_cached_research(ctx.symbol)
        if cached is not None:
            fetched_at, summary, sources = cached
            ttl = self._held_ttl if ctx.already_held else self._ttl
            if datetime.now(timezone.utc) - fetched_at < ttl:
                stamp = fetched_at.astimezone(_KST).strftime("%m-%d %H:%M")
                return ResearchNote(symbol=ctx.symbol, sources=sources,
                                    summary=f"[캐시된 조사 — {stamp} KST 수집] {summary}")
        note = await self._inner.research(ctx)
        if note.summary:
            await self._repo.save_cached_research(ctx.symbol, note.summary, note.sources)
        return note


async def research_candidates(
    candidates: list[CandidateContext],
    provider: ResearchProvider,
    top_n: int | None = None,
) -> list[CandidateContext]:
    """후보에 조사 노트를 붙인다(in-place). top_n 이면 상위 N만(비용 제어)."""
    targets = candidates[:top_n] if top_n is not None else candidates
    for ctx in targets:
        ctx.research = await provider.research(ctx)
    return candidates
