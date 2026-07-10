"""과거 일봉 적재 — FinanceDataReader 로 KRX OHLCV 를 data/history/{symbol}.json 에 저장.

리플레이 백테스트(scripts/backtest.py)의 데이터 소스. 토스 API 와 무관(공개 데이터).
의존성: pip install finance-datareader  (런타임 의존 아님 — 이 스크립트에서만)

실행: server/.venv/Scripts/python server/scripts/fetch_history.py 005930,000660,069500 2024-01-01
      (069500 = 벤치마크 KODEX 200 — backtest.py 기본 벤치마크)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "history"


def main() -> int:
    try:
        import FinanceDataReader as fdr
    except ImportError:
        print("[중단] FinanceDataReader 미설치 — server/.venv/Scripts/pip install finance-datareader")
        return 1
    if len(sys.argv) < 3:
        print("사용법: fetch_history.py <symbols,csv> <start YYYY-MM-DD> [end]")
        return 1
    symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    start, end = sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else None)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        df = fdr.DataReader(sym, start, end)
        bars = [{"date": idx.strftime("%Y-%m-%d"), "open": float(r["Open"]),
                 "high": float(r["High"]), "low": float(r["Low"]),
                 "close": float(r["Close"]), "volume": float(r["Volume"])}
                for idx, r in df.iterrows() if float(r["Close"]) > 0]
        (OUT_DIR / f"{sym}.json").write_text(json.dumps(bars, ensure_ascii=False),
                                             encoding="utf-8")
        print(f"{sym}: {len(bars)}봉 → {OUT_DIR / f'{sym}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
