"""서킷브레이커 — 손실 국면에서 **신규 진입만** 자동 차단(청산은 계속 허용).

손실 관리 설계 결정(TECH-STACK §7 안전 계층)을 결정적 안전장치로 구현: 일일 손실 한도 또는
고점대비 낙폭(MDD) 도달 시 발동. LLM 판단 바깥의 하드 룰이다(가드레일과 같은 철학). 발동해도
**매도(청산)는 막지 않는다** — 포지션을 줄일 길은 항상 열어둔다.

상태를 가진 이유(순수 가드레일과 분리한 이유):
  - **래칭 + 히스테리시스**로 문턱 근처 깜빡임(flapping)을 막아야 한다. 낙폭이 -15%에서 -14.9%로
    잠깐 회복됐다고 바로 재진입을 열면 위험. → 낙폭 발동은 `rearm_drawdown`(예: -8%)까지 회복돼야 해제.
  - **일일 손실 발동은 그날 하루 유지**(당일 반등해도 재진입 금지), 다음 거래일에 자동 리셋
    (토스 `daily_profit_loss.rate` 가 매일 리셋되는 것과 정합).
  - 고점(high-water-mark)은 틱 간 유지되는 상태 → 서비스가 소유하고 매 틱 `assess` 로 갱신.

⚠️ 자기자본(equity)은 KRW 기준(현금 + 보유 평가액 KRW 버킷). 해외분 FX 정규화는 추후 보강 —
equity 를 못 구하면(None) 그 틱의 낙폭 판정은 건너뛴다(허위 리셋 방지).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class CircuitBreakerConfig(BaseModel):
    daily_loss_limit: Decimal = Decimal("0.05")     # 일일 손실률(양수 크기) ≥ 이 값이면 발동
    max_drawdown_limit: Decimal = Decimal("0.15")   # 고점대비 낙폭(양수 크기) ≥ 이 값이면 발동
    rearm_drawdown: Decimal = Decimal("0.08")       # 낙폭 발동 후 이 수준까지 회복돼야 해제(히스테리시스)


class CircuitBreaker:
    """손실 서킷브레이커(상태 보유). 매 틱 `assess` 로 갱신, `tripped` 를 가드레일이 읽는다."""

    def __init__(self, config: CircuitBreakerConfig | None = None):
        self.config = config or CircuitBreakerConfig()
        self.high_water_mark: Decimal | None = None
        self.drawdown: Decimal = Decimal(0)          # 현재 낙폭(양수 크기)
        self._daily_halt_date: date | None = None    # 일일 손실 발동 당일(그날 유지)
        self._drawdown_halt: bool = False            # 낙폭 발동 래치(히스테리시스로 해제)
        self._today: date | None = None              # 마지막 assess 날짜(일일 래치 판정 기준)
        self.reason: str = ""

    @property
    def tripped(self) -> bool:
        return self._drawdown_halt or self._daily_halt_active

    @property
    def _daily_halt_active(self) -> bool:
        return self._daily_halt_date is not None and self._daily_halt_date == self._today

    def assess(
        self,
        equity: Decimal | None,
        daily_pl_rate: Decimal | None,
        now: datetime,
    ) -> bool:
        """현재 자기자본·일일손익률로 상태 갱신 후 발동 여부 반환. 틱당 1회 호출."""
        self._today = now.date()

        # 고점 갱신 + 낙폭 계산 (equity 를 알 때만)
        if equity is not None and equity > 0:
            if self.high_water_mark is None or equity > self.high_water_mark:
                self.high_water_mark = equity
            if self.high_water_mark > 0:
                self.drawdown = max(Decimal(0), (self.high_water_mark - equity) / self.high_water_mark)

        # 낙폭 발동(래칭 + 히스테리시스): equity 를 알 때만 상태 전이
        if equity is not None and self.high_water_mark:
            if self.drawdown >= self.config.max_drawdown_limit:
                self._drawdown_halt = True
            elif self._drawdown_halt and self.drawdown <= self.config.rearm_drawdown:
                self._drawdown_halt = False

        # 일일 손실 발동: 당일 유지(다음 거래일 자동 리셋)
        if daily_pl_rate is not None and daily_pl_rate <= -self.config.daily_loss_limit:
            self._daily_halt_date = now.date()

        self.reason = self._build_reason(daily_pl_rate)
        return self.tripped

    def _build_reason(self, daily_pl_rate: Decimal | None) -> str:
        parts: list[str] = []
        if self._daily_halt_active and daily_pl_rate is not None:
            parts.append(
                f"일일 손실 {daily_pl_rate * 100:.2f}% (한도 -{self.config.daily_loss_limit * 100:.0f}%)"
            )
        if self._drawdown_halt:
            parts.append(
                f"고점대비 낙폭 -{self.drawdown * 100:.2f}% (한도 -{self.config.max_drawdown_limit * 100:.0f}%)"
            )
        return "서킷브레이커 발동 — 신규 진입 중단 (" + ", ".join(parts) + ")" if parts else ""

    def snapshot(self) -> dict:
        """현황 노출용(데스크톱/상태 API)."""
        return {
            "tripped": self.tripped,
            "reason": self.reason,
            "high_water_mark_krw": str(self.high_water_mark) if self.high_water_mark is not None else None,
            "drawdown_pct": f"{self.drawdown * 100:.2f}",
            "daily_halt": self._daily_halt_active,
            "drawdown_halt": self._drawdown_halt,
        }

    # ── 영속화 (재시작 생존 — Cloud Run min=0 은 재시작이 잦다) ────────────────
    def dump_state(self) -> dict:
        """JSON 직렬화 가능한 내부 상태(고점·낙폭·래치). repo.save_engine_state 용."""
        return {
            "high_water_mark": str(self.high_water_mark) if self.high_water_mark is not None else None,
            "drawdown": str(self.drawdown),
            "daily_halt_date": self._daily_halt_date.isoformat() if self._daily_halt_date else None,
            "drawdown_halt": self._drawdown_halt,
            "today": self._today.isoformat() if self._today else None,
        }

    def restore_state(self, state: dict) -> None:
        """dump_state 역직렬화. reason 은 다음 assess 가 재구성(tripped 는 래치로 즉시 유효)."""
        hwm = state.get("high_water_mark")
        self.high_water_mark = Decimal(hwm) if hwm else None
        self.drawdown = Decimal(state.get("drawdown") or "0")
        dhd = state.get("daily_halt_date")
        self._daily_halt_date = date.fromisoformat(dhd) if dhd else None
        self._drawdown_halt = bool(state.get("drawdown_halt", False))
        today = state.get("today")
        self._today = date.fromisoformat(today) if today else None
