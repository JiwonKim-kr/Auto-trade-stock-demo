"""LLM 엔진 라이브 점검 — ClaudeJudge(판단) + WebSearchResearch(조사)를 실제 API 로 1회씩 검증.

지금까지 LLM 경로는 전부 mock 테스트만 통과 → 실가동 전 라이브 1회 검증이 필요하다:
  - 판단: claude-opus-4-8 구조화 출력(action/confidence/rationale)
  - 조사: web_search 도구 호출·인용 수집 (조사 모델은 claude-sonnet-5)
⚠️ 유료 API 호출(판단 1콜 + 조사 1콜: 검색 포함 수백 원 수준). 주문과 무관(읽기 전용 점검).

실행: python server/scripts/llm_live_check.py   (.env 의 ANTHROPIC_API_KEY 필요)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_env() -> None:
    here = Path(__file__).resolve().parent
    for p in (here / ".env", here.parent / ".env"):
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.llm import CandidateContext, ClaudeJudge  # noqa: E402
from app.engine.research import WebSearchResearch  # noqa: E402
from app.engine.screener import ScreenIndicators  # noqa: E402


def sample_context() -> CandidateContext:
    """삼성전자 매수 후보 샘플(지표는 그럴듯한 고정값 — 판단 결과 자체가 아니라 경로 검증이 목적)."""
    return CandidateContext(
        symbol="005930", name="삼성전자", market="KOSPI", currency="KRW",
        indicators=ScreenIndicators(last_close=70000.0, sma_short=69000.0, sma_long=66000.0,
                                    rsi=56.0, avg_volume=12_000_000.0),
        score=0.045, already_held=False,
        recent_closes=[66000, 66500, 67200, 68000, 67800, 68500, 69000, 69400, 69800, 70000],
        market_regime="CALM — 시장 σ 0.8% < 1.0% — 정상",
    )


async def main() -> int:
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[중단] ANTHROPIC_API_KEY 미설정 — server/scripts/.env 에 추가 후 재실행")
        return 1
    ctx = sample_context()

    print("=== 1) 조사 (claude-sonnet-5 + web_search) ===")
    note = await WebSearchResearch().research(ctx)
    print(f"요약: {note.summary[:300]}")
    print(f"출처 {len(note.sources)}건: {note.sources[:3]}")
    ctx.research = note

    print("\n=== 2) 판단 (claude-opus-4-8, 구조화 출력) ===")
    decision = await ClaudeJudge().decide(ctx)
    print(f"action={decision.action.value}  confidence={decision.confidence:.2f}")
    print(f"rationale: {decision.rationale}")

    ok = decision.symbol == "005930" and 0.0 <= decision.confidence <= 1.0
    print(f"\n검증: 심볼 고정={decision.symbol == '005930'} · confidence 범위 OK={ok}")
    print("❗ 주문 미전송(판단 경로 검증만)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
