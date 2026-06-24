"""조사 단계 테스트 (Claude web_search 클라이언트 mock — API 키 불필요)."""

from __future__ import annotations

from app.engine.llm import CandidateContext, ResearchNote, build_user_content
from app.engine.research import (
    NullResearch,
    ResearchConfig,
    WebSearchResearch,
    research_candidates,
)
from app.engine.screener import ScreenIndicators


def ind() -> ScreenIndicators:
    return ScreenIndicators(last_close=70000.0, sma_short=68000.0, sma_long=65000.0,
                            rsi=55.0, avg_volume=1_000_000.0)


def ctx(symbol="005930", **kw) -> CandidateContext:
    base = dict(symbol=symbol, name="삼성전자", market="KOSPI", currency="KRW",
                indicators=ind(), score=0.05, already_held=False)
    base.update(kw)
    return CandidateContext(**base)


# ── mock 클라이언트 ───────────────────────────────────────────────────────────
class _Block:
    def __init__(self, type="text", text=None, url=None):
        self.type = type
        self.text = text
        if url is not None:
            self.url = url


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _SeqClient:
    """호출마다 미리 준 응답을 순서대로 반환(마지막은 반복)."""

    def __init__(self, *responses):
        self.calls: list[dict] = []
        outer = self
        seq = list(responses)

        class _M:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                return seq[min(len(outer.calls) - 1, len(seq) - 1)]

        self.messages = _M()
        self.beta = type("B", (), {"messages": self.messages})()


# ── 테스트 ────────────────────────────────────────────────────────────────────
async def test_web_search_request_shape_and_note():
    resp = _Resp([_Block("text", "삼성전자 4분기 실적 호조. 2026-06 신제품 발표."),
                  _Block("web_search_tool_result", url="https://news.example/1")])
    client = _SeqClient(resp)
    note = await WebSearchResearch(client=client, config=ResearchConfig()).research(ctx())
    rec = client.calls[0]
    assert rec["model"] == "claude-opus-4-8"
    assert rec["tools"][0]["type"] == "web_search_20260209"
    assert rec["tools"][0]["max_uses"] == 4
    assert "리서치 보조자" in rec["system"]
    assert "실적 호조" in note.summary
    assert note.sources == ["https://news.example/1"]


async def test_web_search_handles_pause_turn():
    paused = _Resp([], stop_reason="pause_turn")
    done = _Resp([_Block("text", "특이 공시 없음")], stop_reason="end_turn")
    client = _SeqClient(paused, done)
    note = await WebSearchResearch(client=client).research(ctx())
    assert len(client.calls) == 2          # pause_turn → 이어서 재요청
    assert note.summary == "특이 공시 없음"


async def test_empty_text_falls_back():
    client = _SeqClient(_Resp([]))
    note = await WebSearchResearch(client=client).research(ctx())
    assert note.summary == "특이사항 없음"


async def test_null_research():
    note = await NullResearch().research(ctx())
    assert note.summary == "" and note.sources == []


async def test_research_candidates_respects_top_n():
    class FakeProvider:
        def __init__(self):
            self.seen: list[str] = []

        async def research(self, c):
            self.seen.append(c.symbol)
            return ResearchNote(symbol=c.symbol, summary=f"{c.symbol} 브리프")

    p = FakeProvider()
    cands = [ctx(symbol=s) for s in ("A", "B", "C")]
    out = await research_candidates(cands, p, top_n=2)
    assert p.seen == ["A", "B"]                       # 상위 2만 조사
    assert out[0].research.summary == "A 브리프"
    assert out[2].research is None                    # C는 미조사


def test_build_user_content_includes_research_section():
    c = ctx(research=ResearchNote(symbol="005930", summary="신제품 발표로 거래량 급증",
                                  sources=["https://a", "https://b"]))
    text = build_user_content(c)
    assert "[조사] 신제품 발표로 거래량 급증 (출처 2건)" in text
