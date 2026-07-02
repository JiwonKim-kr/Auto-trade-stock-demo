"""시장 레짐 필터 — 거시/변동성 국면 → 결정적 노출 배수 (study.md §3.3 이식).

원칙: **거시·지정학 이벤트는 알파(예측 시그널)가 아니라 리스크 필터**다. 전쟁·금리·규제 같은
사건은 10년에 몇 번뿐이라 통계 검증이 불가하고 비정상적이다 → 예측하지 않고 **대응**한다:
시장 프록시(기본 KODEX 200)의 실현변동성이 높은 국면엔 신규 매수 노출을 결정적으로 줄인다.

    CALM    (σ < calm_vol)    → ×1.0  (정상 사이징)
    ELEVATED(calm ≤ σ < stress) → ×0.5  (신규 노출 절반)
    STRESS  (σ ≥ stress_vol)  → ×0.0  (신규 진입 중단 — 청산은 무관)

배수는 allocator 의 매수 목표금액에만 곱한다(**매도(청산) 경로는 절대 축소하지 않는다** —
서킷브레이커와 같은 철학: 위험 국면에도 포지션을 줄일 길은 항상 열어둔다).
σ 계산은 costs.realized_daily_vol 재활용(일간 수익률 표준편차). 표본 부족이면 UNKNOWN → ×1.0
(레짐은 시장 전체 오버레이라 조회 실패로 전 매수를 막으면 취약 — 종목별 안전은 진입 게이트·
가드레일이 이미 fail-closed 로 지킨다).

⚠️ 임계값 기본(일간 σ): calm 1% · stress 2% — KOSPI 평시/위기 대략치. 튜너블(settings).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from app.engine.costs import realized_daily_vol


class RegimeLevel(str, Enum):
    CALM = "CALM"
    ELEVATED = "ELEVATED"
    STRESS = "STRESS"
    UNKNOWN = "UNKNOWN"       # 표본 부족/조회 실패 — 배수 1.0 (게이트·가드레일이 종목별 방어)


class RegimeConfig(BaseModel):
    symbol: str = "069500"                             # 시장 프록시(KODEX 200 ETF)
    lookback: int = 20                                 # σ 추정에 쓸 일간 수익률 수
    calm_vol: Decimal = Decimal("0.010")               # 일간 σ < 1% → CALM
    stress_vol: Decimal = Decimal("0.020")             # 일간 σ ≥ 2% → STRESS
    elevated_multiplier: Decimal = Decimal("0.5")
    stress_multiplier: Decimal = Decimal("0")


@dataclass
class RegimeAssessment:
    level: RegimeLevel
    daily_vol: Decimal | None
    multiplier: Decimal
    reason: str

    def as_dict(self) -> dict:
        """직렬화(틱 기록/API 응답용)."""
        return {
            "level": self.level.value,
            "daily_vol": str(self.daily_vol) if self.daily_vol is not None else None,
            "multiplier": str(self.multiplier),
            "reason": self.reason,
        }


def assess_regime(closes: Sequence[float] | None, config: RegimeConfig | None = None) -> RegimeAssessment:
    """시장 프록시 종가 경로 → 레짐 판정. 순수 함수(조회는 호출자 책임)."""
    cfg = config or RegimeConfig()
    window = list(closes)[-(cfg.lookback + 1):] if closes else None
    vol = realized_daily_vol(window, min_returns=5)
    if vol is None:
        return RegimeAssessment(RegimeLevel.UNKNOWN, None, Decimal(1),
                                "레짐 판정 불가(표본 부족) — 배수 1.0")
    if vol >= cfg.stress_vol:
        return RegimeAssessment(RegimeLevel.STRESS, vol, cfg.stress_multiplier,
                                f"시장 σ {vol * 100:.2f}% ≥ {cfg.stress_vol * 100:.1f}% — 신규 진입 중단")
    if vol >= cfg.calm_vol:
        return RegimeAssessment(RegimeLevel.ELEVATED, vol, cfg.elevated_multiplier,
                                f"시장 σ {vol * 100:.2f}% ≥ {cfg.calm_vol * 100:.1f}% — 노출 축소")
    return RegimeAssessment(RegimeLevel.CALM, vol, Decimal(1),
                            f"시장 σ {vol * 100:.2f}% < {cfg.calm_vol * 100:.1f}% — 정상")
