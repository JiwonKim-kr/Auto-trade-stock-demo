"""리플레이 백테스트 CLI — data/history/*.json 으로 결정적 전략을 과거 구간에서 검증.

⚠️ 결과 해석 규율(엔진 docstring 참조): LLM 알파는 소급 평가 불가(판단기 Deterministic 전용),
생존편향으로 절대 성과는 상향 편향 — **파라미터 상대 비교·구성요소 검증 용도**로만.

실행: server/.venv/Scripts/python server/scripts/backtest.py [벤치마크심볼=069500]
사전: scripts/fetch_history.py 로 data/history/ 적재.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.replay import run_backtest  # noqa: E402

HISTORY_DIR = Path(__file__).resolve().parents[1] / "data" / "history"


async def main() -> int:
    benchmark = sys.argv[1] if len(sys.argv) > 1 else "069500"
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        print(f"[중단] {HISTORY_DIR} 비어있음 — scripts/fetch_history.py 로 적재 먼저")
        return 1
    histories = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in files}
    print(f"리플레이 백테스트 — {len(histories)}종목"
          f" (벤치마크 {benchmark if benchmark in histories else '없음'}) · LLM 불사용(규율)\n")
    r = await run_backtest(histories, benchmark=benchmark if benchmark in histories else None)
    d = r.eval_report.as_dict()
    print(f"거래일 {len(r.equity_curve)} · 매수 체결 {r.buys} · 미체결 소멸 {r.unfilled}"
          f" · 완결 트레이드 N={r.trade_count}")
    print(f"누적수익 {d['cumulative_return']} · MDD {d['mdd']} · Sharpe(연) {d['sharpe_annual']}"
          f" · 벤치마크 {d['benchmark_return']} · 초과 {d['excess_return']}")
    print(f"판정: {d['verdict']}")
    print("\n⚠️ 생존편향·LLM 제외 — 절대 성과가 아니라 파라미터 상대 비교 용도")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
