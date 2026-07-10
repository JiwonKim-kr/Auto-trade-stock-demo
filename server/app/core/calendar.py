"""KRX 거래일 캘린더 — 주말 + 공휴일(data/krx_holidays.json) 판정 (PLAN §3.6).

파일에 해당 연도 키가 없으면 **경고 로그 + 평일=거래일 폴백**(조용한 실패 금지 — 공휴일 틱은
가드레일이 주문을 막으므로 안전은 유지되고, 비용·보고서 트리거 정확도만 떨어진다).
갱신 절차: 매년 말 KRX 공지 확인 → data/krx_holidays.json 에 연도 키 추가.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger("app.calendar")

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "krx_holidays.json"
_warned_years: set[str] = set()


def load_holidays(path: str | Path | None = None) -> frozenset[str]:
    """휴장일(YYYY-MM-DD) 집합 로드. 파일 없으면 빈 집합 + 경고."""
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        logger.warning("휴장일 파일 없음(%s) — 주말만 휴장으로 판정", p)
        return frozenset()
    data = json.loads(p.read_text(encoding="utf-8"))
    days: set[str] = set()
    for year, items in data.items():
        if year.startswith("_"):
            continue
        days.update(items)
    return frozenset(days)


def is_trading_day(d: date, holidays: frozenset[str]) -> bool:
    """KRX 거래일 여부(주말·공휴일 제외). 연도 미등재는 1회 경고 후 평일=거래일 폴백."""
    if d.weekday() >= 5:
        return False
    iso = d.isoformat()
    year = iso[:4]
    if holidays and not any(h.startswith(year) for h in holidays) and year not in _warned_years:
        _warned_years.add(year)
        logger.warning("%s년 휴장일 미등재 — data/krx_holidays.json 갱신 필요(평일=거래일 폴백)", year)
    return iso not in holidays
