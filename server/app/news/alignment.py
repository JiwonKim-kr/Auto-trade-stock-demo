"""t+1 정렬 규칙(§8.3) — 논문 방법론 섹션에 그대로 전사하는 순수 함수.

보도 시각(KST)이 거래일 15:30 이전이면 (당일 종가 → t+1 종가),
그 외(장 마감 후·주말·공휴일)면 (t+1 시가 → t+2 시가). t+n 은 거래일 기준
(§3.6 KRX 캘린더) — 금요일 15:30 후 보도는 월요일 시가 → 화요일 시가.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.core.calendar import is_trading_day

KST = timezone(timedelta(hours=9))
MARKET_CLOSE = time(15, 30)


def next_trading_day(d: date, holidays: frozenset[str]) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n, holidays):
        n += timedelta(days=1)
    return n


@dataclass(frozen=True)
class ReturnWindow:
    entry_date: date
    entry_field: str      # "close" | "open"
    exit_date: date
    exit_field: str
    rule: str             # 방법론 전사용 설명


def return_window(published: datetime, holidays: frozenset[str]) -> ReturnWindow:
    """보도 시각(tz-aware) → 수익률 측정 구간. 시장 조정은 같은 구간의 지수 수익률 차감(§8.3)."""
    p = published.astimezone(KST)
    d = p.date()
    if is_trading_day(d, holidays) and p.time() < MARKET_CLOSE:
        return ReturnWindow(d, "close", next_trading_day(d, holidays), "close",
                            "장중 보도 → 당일 종가→t+1 종가")
    t1 = next_trading_day(d, holidays)
    return ReturnWindow(t1, "open", next_trading_day(t1, holidays), "open",
                        "마감 후/휴장 보도 → t+1 시가→t+2 시가")
