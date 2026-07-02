"""비용 모델 + 비용 인지 진입 게이트 — 엣지가 거래비용을 못 넘는 매수를 결정적으로 차단.

study.md §3.4 이식: **기대이동폭 ≥ 라운드트립 비용 × 배수(기본 3.5)** 일 때만 신규 매수.
비용에 갉아먹히는 잔매매를 막는다. LLM 판단 바깥의 결정적 필터(가드레일과 같은 철학).

⚠️ 핵심 설계 난점 — **LLM은 방향+confidence만 주고 기대이동폭(magnitude)을 주지 않는다**(의도된 설계:
실자금에서 LLM 숫자 신뢰 최소화). 그래서 기대이동폭을 결정적 **프록시**로 추정한다:

    기대이동폭 = confidence × 일간 실현변동성(σ) × move_multiple

  - σ = 최근 종가 경로의 일간 단순수익률 표준편차(유동성/변동성 큰 종목일수록 기대이동↑).
  - move_multiple = 보유 호라이즌(수 일~수 주) 동안 favorable 이동을 몇 σ로 볼지의 튜너블(기본 3).
  이는 정밀 예측이 아니라 **휴리스틱 프록시**다 — 목적은 "비용 대비 엣지가 얕은 매수 차단".

비용(라운드트립, 매수+매도 왕복):  2×수수료 + 2×슬리피지 + 매도세(증권거래세, 매도에만).
  기본값 ≈ 0.48% → 진입 문턱(×3.5) ≈ 1.68%.
⚠️ 증권거래세: 2025~ KOSPI/KOSDAQ 0.15%(농특세 포함) 기준. 세율·제도는 변동 → **실거래 시 재확인**
  (study.md의 "0.20% 부활"은 확인 결과 인하 경로 0.15%와 상충 — 보수적으로 높게 잡으려면 설정에서 상향).
슬리피지는 유동성별로 크게 다르다(동전주·코스닥 소형주 스프레드↑) — 종목별 보정은 지능형 사전선별 이후.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel


class CostConfig(BaseModel):
    commission_rate: Decimal = Decimal("0.00015")   # 편도 수수료
    slippage_rate: Decimal = Decimal("0.0015")      # 편도 슬리피지/스프레드(유동성별 보정 대상)
    sell_tax_rate: Decimal = Decimal("0.0015")      # 증권거래세(매도에만). ⚠️실거래 시 재확인

    def round_trip_rate(self) -> Decimal:
        """왕복 비용률(매수 진입 + 매도 청산). 매도세는 청산 시 1회."""
        return 2 * self.commission_rate + 2 * self.slippage_rate + self.sell_tax_rate


class EntryGateConfig(BaseModel):
    cost_multiple: Decimal = Decimal("3.5")   # 진입 문턱 = 라운드트립 × 이 값
    move_multiple: Decimal = Decimal("3.0")   # 기대이동폭 = confidence × σ × 이 값(호라이즌 프록시)
    min_returns: int = 5                       # 변동성 추정 최소 수익률 표본(부족하면 보수적 차단)


@dataclass
class GateResult:
    passed: bool
    expected_move_rate: Decimal
    hurdle_rate: Decimal
    vol_rate: Decimal | None
    reason: str


def realized_daily_vol(closes: Sequence[float] | None, min_returns: int = 5) -> Decimal | None:
    """최근 종가 경로 → 일간 단순수익률 표준편차(σ). 표본 부족/무효면 None."""
    if not closes or len(closes) < 2:
        return None
    rets: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0:
            rets.append(cur / prev - 1.0)
    if len(rets) < max(2, min_returns):
        return None
    return Decimal(str(statistics.stdev(rets)))


class EntryGate:
    """비용 인지 진입 게이트. 매수 후보의 기대이동폭이 문턱(라운드트립×배수)을 넘는지 결정적으로 판정."""

    def __init__(self, cost: CostConfig | None = None, config: EntryGateConfig | None = None):
        self.cost = cost or CostConfig()
        self.config = config or EntryGateConfig()

    def hurdle_rate(self) -> Decimal:
        return self.cost.round_trip_rate() * self.config.cost_multiple

    def evaluate(self, confidence: float, closes: Sequence[float] | None) -> GateResult:
        hurdle = self.hurdle_rate()
        vol = realized_daily_vol(closes, self.config.min_returns)
        if vol is None:
            return GateResult(False, Decimal(0), hurdle, None,
                              "변동성 추정 불가(표본 부족) — 보수적 차단")
        conf = Decimal(str(max(0.0, min(1.0, confidence))))
        expected = conf * vol * self.config.move_multiple
        passed = expected >= hurdle
        reason = (f"기대이동 {expected * 100:.2f}% {'≥' if passed else '<'} "
                  f"문턱 {hurdle * 100:.2f}% (σ {vol * 100:.2f}%·conf {conf})")
        return GateResult(passed, expected, hurdle, vol, reason)
