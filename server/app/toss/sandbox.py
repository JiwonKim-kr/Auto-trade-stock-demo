"""샌드박스 시세 어댑터 — 토스 API 호출 0으로 **상시 틱**을 돌리기 위한 합성 클라이언트.

용도: 전 파이프라인(스크리너→게이트→레짐→판단→청산→페이퍼 체결→DB→대시보드)을 비용·
실계좌 의존 없이 계속 돌려 관측한다. `engine/stress.py` 의 SyntheticToss 가 시나리오 1회용
(get_stocks/get_candles만)인 것과 달리, 이쪽은 토스 클라이언트 인터페이스 전체를 덕타이핑해
`execute_tick` 이 그대로 동작한다.

시세 모형: 종목별 시드 고정 랜덤워크(같은 seed → 같은 경로, 재현 가능). 시뮬 일자는
**경과 실시간**으로 진행(day_seconds 마다 +1일) — 틱이 돌 때마다 새 봉이 보인다.

⚠️ 이 어댑터는 실계좌와 무관하다. 보유는 항상 비어 있고(페이퍼 장부가 포지션을 만든다),
매수가능금액은 고정값이다. 샌드박스 DB 를 운영 DB 와 반드시 분리할 것(main.py 가 강제).
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.toss.models import BuyingPower, Candle, Holdings, Price, Stock

KST = timezone(timedelta(hours=9))
_EMPTY_HOLDINGS = {
    "totalPurchaseAmount": {"krw": "0"},
    "marketValue": {"amount": {"krw": "0"}},
    "profitLoss": {"amount": {"krw": "0"}, "rate": "0"},
    "items": [],
}


def _stable_seed(symbol: str, seed: int) -> int:
    """프로세스 간 안정적인 종목별 시드(str.__hash__ 는 실행마다 달라 못 씀)."""
    h = hashlib.blake2b(symbol.encode("utf-8"), digest_size=4).digest()
    return (int.from_bytes(h, "big") ^ seed) & 0x7FFFFFFF


class SandboxToss:
    """토스 클라이언트 덕타이핑 합성 구현 — 상시 틱 샌드박스용."""

    def __init__(self, *, seed: int = 42, day_seconds: int = 60, history: int = 60,
                 warmup_days: int = 60, trend: float = 0.0015, vol: float = 0.016,
                 buying_power_krw: Decimal = Decimal("10000000"),
                 started_at: datetime | None = None):
        self.seed = seed
        self.day_seconds = max(1, day_seconds)
        self.history = history
        # 종목별 고유 추세(평균 trend, 편차 0.3%p) — 오르는·횡보·빠지는 종목이 섞인다.
        # 추세가 없으면 스크리너 score→confidence 가 낮아 **전부 비용 게이트에서 거부**되고
        # 샌드박스가 거부 경로만 검증하게 된다(stress.py 가 랠리 경로를 쓰는 것과 같은 이유).
        self.trend = trend
        self.vol = vol
        # 워밍업: 시뮬을 0일이 아니라 이 지점부터 시작한다 — 첫 틱부터 스크리너 지표
        # (SMA/RSI/ADV20)가 산출될 만큼의 히스토리가 이미 존재해야 후보가 나온다.
        self.warmup_days = warmup_days
        self.buying_power_krw = buying_power_krw
        self._started = started_at or datetime.now(timezone.utc)
        self._paths: dict[str, list[float]] = {}
        self._drift: dict[str, float] = {}

    # ── 합성 가격 경로 ────────────────────────────────────────────────────────
    def _day(self) -> int:
        """워밍업 + 경과 실시간 → 시뮬 일자(틱마다 새 봉이 생기도록)."""
        elapsed = (datetime.now(timezone.utc) - self._started).total_seconds()
        return self.warmup_days + int(elapsed // self.day_seconds)

    def _path(self, symbol: str, upto: int) -> list[float]:
        """종목별 랜덤워크(시드 고정). 필요 길이까지 확장하며 캐시."""
        path = self._paths.get(symbol)
        if path is None:
            rng = random.Random(_stable_seed(symbol, self.seed))
            start = 5_000 + rng.random() * 95_000          # 5천~10만원대
            self._drift[symbol] = rng.gauss(self.trend, 0.003)
            path = [start]
            self._paths[symbol] = path
        if len(path) <= upto:
            drift = self._drift[symbol]
            rng = random.Random(_stable_seed(symbol, self.seed) + len(path))
            while len(path) <= upto:
                path.append(max(100.0, path[-1] * (1 + rng.gauss(drift, self.vol))))
        return path

    def _bars(self, symbol: str) -> list[tuple[datetime, float]]:
        day = self._day()
        path = self._path(symbol, day)
        lo = max(0, day - self.history + 1)
        base = datetime.now(KST).replace(hour=15, minute=30, second=0, microsecond=0)
        return [(base - timedelta(days=day - i), path[i]) for i in range(lo, day + 1)]

    def last_price(self, symbol: str) -> Decimal:
        return Decimal(str(round(self._path(symbol, self._day())[self._day()], 1)))

    # ── 토스 클라이언트 인터페이스 ────────────────────────────────────────────
    async def get_stocks(self, symbols) -> list[Stock]:
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        return [Stock(symbol=s, name=f"샌드박스{s}", market="KOSPI", currency="KRW",
                      is_common_share=True, status="ACTIVE") for s in syms]

    async def get_candles(self, symbol: str, interval: str = "1d") -> list[Candle]:
        out = []
        for i, (ts, close) in enumerate(self._bars(symbol)):
            rng = random.Random(_stable_seed(symbol, self.seed) + i)
            spread = close * (0.002 + rng.random() * 0.01)
            out.append(Candle(timestamp=ts.isoformat(), open_price=close - spread / 2,
                              high_price=close + spread, low_price=close - spread,
                              close_price=close, volume=int(80_000 + rng.random() * 2_000_000),
                              currency="KRW"))
        return out

    async def get_prices(self, symbols) -> list[Price]:
        syms = symbols if isinstance(symbols, list) else str(symbols).split(",")
        now = datetime.now(KST)
        return [Price(symbol=s, timestamp=now, last_price=self.last_price(s), currency="KRW")
                for s in syms]

    async def get_holdings(self) -> Holdings:
        """항상 비어 있음 — 포지션은 페이퍼 장부가 만든다(실계좌 무관)."""
        return Holdings.model_validate(_EMPTY_HOLDINGS)

    async def get_buying_power(self, currency: str = "KRW") -> BuyingPower:
        return BuyingPower.model_validate(
            {"currency": currency.upper(), "cashBuyingPower": str(self.buying_power_krw)})

    async def get_stock_warnings(self, symbol: str) -> list:
        return []                                   # 샌드박스엔 경고 종목 없음

    async def aclose(self) -> None:
        return None
