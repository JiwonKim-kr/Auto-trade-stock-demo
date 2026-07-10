"""캔들 TTL 캐시 래퍼 — get_candles 만 캐시, 나머지는 위임 (IMPLEMENTATION-PLAN §2.1).

일봉은 장중에 마지막(진행 중) 봉만 바뀐다 — 유니버스 40종목 × 78틱/일 ≈ 3,120콜은 BASIC tier
429 의 주범. TTL(기본 60분) 캐시로 ≈ 240콜/일(92% 절감). 트레이드오프: 스크리너 신호가 최대
TTL 만큼 지연 — 일봉 SMA/RSI 신호는 하루 단위라 영향 미미.

주입형(§0-6): 경계(api/tick.py)가 run_tick 에 넘기는 toss 만 감싼다. **리컨실 holdings ·
페이퍼 마킹 prices 는 감싸지 않는다**(실시간성 필요 — get_candles 만 캐시 대상이라 자연 충족).
pipeline 은 덕타이핑이라 무변경.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.toss.models import Candle


class CachingToss:
    """toss 클라이언트 래퍼. get_candles 캐시 히트 시 원본 API 를 호출하지 않는다."""

    def __init__(self, inner, repo, ttl_minutes: int = 60):
        self._inner = inner
        self._repo = repo
        self._ttl = timedelta(minutes=ttl_minutes)

    async def get_candles(self, symbol: str, interval: str = "1d") -> list[Candle]:
        cached = await self._repo.get_cached_candles(symbol, interval)
        if cached is not None:
            fetched_at, payload = cached
            if datetime.now(timezone.utc) - fetched_at < self._ttl:
                return [Candle.model_validate(c) for c in payload]
        candles = await self._inner.get_candles(symbol, interval)
        await self._repo.save_cached_candles(
            symbol, interval, [c.model_dump(mode="json") for c in candles])
        return candles

    def __getattr__(self, name):
        return getattr(self._inner, name)   # holdings/prices/stocks 등은 전부 위임
