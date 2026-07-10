"""confidence 캘리브레이션 리포트 — DB 의 판단 이력 × 캔들 캐시로 사후 수익률 버킷 분석.

BUY 판단(판단 시점 가격 보존분)의 t+5·t+20 거래일 수익률을 confidence 버킷별로 집계한다.
해석: 버킷 평균 수익률이 **단조 증가**해야 confidence 가 사이징 입력으로 적합. 비단조면
allocator 를 계단 함수(예: <0.6 스킵 · 0.6~0.8 half · >0.8 full)로 교체 검토(PLAN §5).

실행: server/.venv/Scripts/python server/scripts/calibration_report.py  (.env 의 DATABASE_URL 필요)
데이터 원천: decisions.decision_price + candle_cache(캐시가 최신 봉을 갖고 있어야 함 — 상시 운용 중 자동 축적).
"""

from __future__ import annotations

import asyncio
import json
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

from sqlalchemy import select  # noqa: E402

from app.engine.calibration import bucket_calibration, forward_return, is_monotonic  # noqa: E402
from app.db.models import CandleCacheRow, DecisionRow, TickRow  # noqa: E402
from app.db.session import make_engine, make_sessionmaker  # noqa: E402


async def main() -> int:
    load_env()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("[중단] DATABASE_URL 미설정 — 캘리브레이션은 판단 이력(DB)이 필요")
        return 1
    engine = make_engine(url)
    sm = make_sessionmaker(engine)
    async with sm() as s:
        rows = (await s.execute(
            select(TickRow.trade_date, DecisionRow.symbol, DecisionRow.confidence,
                   DecisionRow.decision_price)
            .join(TickRow, DecisionRow.tick_id == TickRow.id)
            .where(DecisionRow.action == "BUY", DecisionRow.decision_price.is_not(None))
        )).all()
        caches = {r.symbol: json.loads(r.payload_json) for r in
                  (await s.execute(select(CandleCacheRow))).scalars().all()}
    await engine.dispose()

    closes_by_symbol: dict[str, list[tuple[str, float]]] = {}
    for sym, payload in caches.items():
        pts = sorted((c["timestamp"][:10], float(c["close_price"])) for c in payload)
        closes_by_symbol[sym] = pts

    for horizon in (5, 20):
        samples = []
        for trade_date, sym, conf, price in rows:
            closes = closes_by_symbol.get(sym)
            if not closes:
                continue
            r = forward_return(closes, trade_date, float(price), horizon)
            if r is not None:
                samples.append((conf, r))
        stats = bucket_calibration(samples)
        print(f"\n=== BUY 판단 t+{horizon} 거래일 수익률 (표본 {len(samples)}) ===")
        if not stats:
            print("  (표본 없음 — 판단·캔들 캐시가 더 쌓여야 함)")
            continue
        for st in stats:
            print("  " + st.row())
        verdict = "단조 증가 ✓ — confidence 를 사이징 입력으로 유지" if is_monotonic(stats) \
            else "비단조 ✗ — 계단 사이징(<0.6 스킵·0.6~0.8 half·>0.8 full) 교체 검토(PLAN §5)"
        print(f"  판정: {verdict} (버킷당 n≥30 전엔 참고용)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
