"""스트레스 샌드박스 CLI — 합성 시나리오(폭락·갭·횡보·랠리)로 안전장치 체인 검증.

토스 API·DB·LLM 없이 로컬에서 즉시 실행. 2부 구성:
  1) 전 방어층 활성 — 게이트·레짐·손절·서킷브레이커가 겹겹이 손실을 흡수하는지
  2) 최소 방어(CB만) — 앞 층이 흡수하면 CB 까지 도달하지 않으므로, 최후 방어선 경로는
     앞 층을 끈 적대 조건에서 별도 증명한다(발동 + 이후 신규 매수 0).
마지막 열(CB후 매수)이 0이 아니면 안전 결함.

실행: server/.venv/Scripts/python server/scripts/stress_sim.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.stress import SCENARIOS, simulate  # noqa: E402


async def main() -> int:
    print("합성 스트레스 샌드박스 — 안전장치 체인 검증 (실주문 0 · 네트워크 0)\n")
    ok = True

    print("[1] 전 방어층 활성 (게이트·레짐·손절·CB)")
    for sc in SCENARIOS:
        r = await simulate(sc)
        print(r.row())
        ok &= r.buys_after_trip == 0

    print("\n[2] 최소 방어 (서킷브레이커만 — 최후 방어선 경로 증명)")
    cb_proven = False
    for sc in SCENARIOS[:2]:
        r = await simulate(sc, minimal_defenses=True)
        print(r.row())
        ok &= r.buys_after_trip == 0
        cb_proven |= r.cb_tripped_day is not None
    if not cb_proven:
        ok = False
        print("❌ 최소 방어에서도 CB 미발동 — 최후 방어선 경로 미증명")

    print("\n판정:", "✅ CB 발동 증명 + 전 시나리오 CB 이후 신규 매수 0" if ok
          else "❌ 안전 결함 발견 — 위 표 확인")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
