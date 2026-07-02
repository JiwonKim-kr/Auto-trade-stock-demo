"""평가 — 페이퍼 자산곡선의 통계적 판정 (study.md §2.2/§3.4/§8 규율의 이식).

원칙:
  - **모든 수익률은 넷** — 입력 자산곡선이 이미 비용 차감 후(페이퍼 체결이 비용 반영).
  - **절대수익이 아니라 벤치마크 대비 + 위험조정** — 시장 베타를 알파로 착각하지 않는다.
  - **표본이 부족하면 판단하지 말 것** — 완결 트레이드 N<100 이면 "판단 보류"
    (샤프 표준오차 SE ≈ √((1+0.5·SR²)/N): N 이 작으면 운/실력 구분 불가).

한계(정직하게):
  - SE 공식은 iid 정규 가정 — 수익률 자기상관·팻테일이면 불확실성 과소평가(Lo 보정 TODO).
  - 다중 시그널 시험 시 Deflated Sharpe(다중검정 보정) 필요. (TODO)
  - 벤치마크 대비는 단순 차이(베타 미조정) — 정식 알파(회귀)는 표본이 쌓인 뒤.
지표 계산은 통계량이라 float 사용(자금 이동 아님 — Decimal 불변식은 장부에만 적용).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

TRADING_DAYS = 252
MIN_TRADES_FOR_JUDGEMENT = 100


@dataclass
class EvalReport:
    n_days: int                            # 일일 수익률 표본 수
    n_trades: int                          # 완결 왕복(매도) 수
    cumulative_return: float | None        # 넷 누적수익률
    sharpe_annual: float | None            # 연환산(√252)
    sharpe_se_annual: float | None         # SE(iid 가정) 연환산
    mdd: float | None                      # 최대 낙폭(양수 크기)
    benchmark_return: float | None         # 같은 구간 벤치마크 누적수익률
    excess_return: float | None            # 단순 차이(베타 미조정)
    verdict: str
    caveat: str = "SE는 iid 가정(자기상관 시 과소평가) · 벤치마크 대비는 베타 미조정 단순 차이"

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "n_days", "n_trades", "cumulative_return", "sharpe_annual", "sharpe_se_annual",
            "mdd", "benchmark_return", "excess_return", "verdict", "caveat")}


def _returns(values: list[float]) -> list[float]:
    return [cur / prev - 1.0 for prev, cur in zip(values, values[1:]) if prev > 0]


def _mdd(values: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def evaluate(
    daily_equity: list[tuple[str, Decimal]],
    daily_benchmark: list[tuple[str, Decimal | None]] | None = None,
    n_trades: int = 0,
    min_trades: int = MIN_TRADES_FOR_JUDGEMENT,
) -> EvalReport:
    """일일 자산곡선(날짜 오름차순, 하루 1점) → 지표 + 판정. 순수 함수."""
    equity = [float(e) for _, e in daily_equity]
    rets = _returns(equity)
    n = len(rets)

    if n < 2:
        return EvalReport(n_days=n, n_trades=n_trades, cumulative_return=None,
                          sharpe_annual=None, sharpe_se_annual=None, mdd=None,
                          benchmark_return=None, excess_return=None,
                          verdict="평가 불가 — 일일 수익률 표본 부족(2일 미만)")

    cumulative = equity[-1] / equity[0] - 1.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    std = math.sqrt(var)
    sharpe_d = mean / std if std > 0 else None
    sharpe_a = sharpe_d * math.sqrt(TRADING_DAYS) if sharpe_d is not None else None
    se_a = (math.sqrt((1 + 0.5 * sharpe_d**2) / n) * math.sqrt(TRADING_DAYS)
            if sharpe_d is not None else None)

    bench = None
    if daily_benchmark:
        prices = [float(p) for _, p in daily_benchmark if p is not None]
        if len(prices) >= 2 and prices[0] > 0:
            bench = prices[-1] / prices[0] - 1.0
    excess = cumulative - bench if bench is not None else None

    if n_trades < min_trades:
        verdict = (f"판단 보류 — 완결 트레이드 N={n_trades} < {min_trades} "
                   "(표본 부족: 운/실력 구분 불가)")
    elif sharpe_a is None:
        verdict = "판단 불가 — 수익률 변동 없음(σ=0)"
    elif abs(sharpe_a) < 2 * se_a:
        verdict = f"유의성 부족 — |SR {sharpe_a:.2f}| < 2×SE {se_a:.2f} (운과 구분 불가)"
    else:
        verdict = "표본·유의성 기준 충족 — 벤치마크 대비·MDD 를 함께 검토"

    return EvalReport(n_days=n, n_trades=n_trades, cumulative_return=cumulative,
                      sharpe_annual=sharpe_a, sharpe_se_annual=se_a, mdd=_mdd(equity),
                      benchmark_return=bench, excess_return=excess, verdict=verdict)
