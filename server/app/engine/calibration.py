"""LLM confidence 캘리브레이션 — 판단의 사후 수익률로 confidence 의 정보량을 측정 (PLAN §2.3).

사이징이 `ceiling × confidence` 로 LLM 숫자를 단조 신뢰한다 → 그 신뢰가 정당한지 데이터로 검증:
confidence 버킷별 BUY 판단의 t+h 거래일 수익률·승률이 **버킷과 단조 증가**해야 사이징 입력으로
적합하다. 비단조면 allocator 를 계단 함수로 교체 검토(PLAN §5). 순수 계산만 — 조회는 스크립트가.
"""

from __future__ import annotations

from dataclasses import dataclass

BUCKETS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def forward_return(daily_closes: list[tuple[str, float]], decision_date: str,
                   decision_price: float, horizon: int) -> float | None:
    """판단일 **이후** h번째 거래일 종가 대비 수익률. 미래 봉이 부족하면 None(미성숙 판단 제외).

    daily_closes: (YYYY-MM-DD, close) 날짜 오름차순 — 판단일 당일 봉은 제외(미래정보 금지).
    """
    if decision_price <= 0 or horizon < 1:
        return None
    future = [c for d, c in daily_closes if d > decision_date]
    if len(future) < horizon:
        return None
    return future[horizon - 1] / decision_price - 1.0


@dataclass
class BucketStat:
    bucket: str          # "0.6~0.7"
    n: int
    avg_return: float
    win_rate: float

    def row(self) -> str:
        return f"{self.bucket:>8} | n={self.n:>4} | 평균 {self.avg_return * 100:+6.2f}% | 승률 {self.win_rate * 100:5.1f}%"


def bucket_calibration(samples: list[tuple[float, float]]) -> list[BucketStat]:
    """(confidence, forward_return) → 버킷별 통계. 표본 없는 버킷은 생략."""
    out: list[BucketStat] = []
    for lo, hi in BUCKETS:
        rets = [r for c, r in samples if lo <= c < hi]
        if not rets:
            continue
        out.append(BucketStat(
            bucket=f"{lo:.1f}~{min(hi, 1.0):.1f}", n=len(rets),
            avg_return=sum(rets) / len(rets),
            win_rate=sum(1 for r in rets if r > 0) / len(rets)))
    return out


def is_monotonic(stats: list[BucketStat]) -> bool:
    """버킷 평균 수익률이 단조 비감소인가 — 캘리브레이션 적합성의 1차 판정."""
    rets = [s.avg_return for s in stats]
    return all(a <= b for a, b in zip(rets, rets[1:]))
