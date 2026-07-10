"""캘리브레이션 테스트 — 사후 수익률(미래정보 금지)·버킷 집계·단조성 판정."""

from __future__ import annotations

from app.engine.calibration import (
    BucketStat,
    bucket_calibration,
    forward_return,
    is_monotonic,
)

CLOSES = [("2026-07-01", 100.0), ("2026-07-02", 102.0), ("2026-07-03", 104.0),
          ("2026-07-06", 106.0), ("2026-07-07", 108.0), ("2026-07-08", 110.0)]


def test_forward_return_uses_only_future_days():
    # 판단일(7/2) 당일 봉 제외 — t+1 = 7/3(104) → 104/100 - 1
    assert forward_return(CLOSES, "2026-07-02", 100.0, 1) == 104.0 / 100.0 - 1.0
    assert forward_return(CLOSES, "2026-07-02", 100.0, 4) == 110.0 / 100.0 - 1.0


def test_forward_return_none_when_immature():
    assert forward_return(CLOSES, "2026-07-07", 100.0, 5) is None    # 미래 봉 부족 → 표본 제외
    assert forward_return(CLOSES, "2026-07-02", 0.0, 1) is None      # 가격 0 방어


def test_bucket_calibration_groups_and_winrate():
    samples = [(0.55, 0.01), (0.58, -0.02), (0.85, 0.05), (0.87, 0.03), (0.99, 0.10)]
    stats = bucket_calibration(samples)
    by = {s.bucket: s for s in stats}
    assert by["0.5~0.6"].n == 2 and by["0.5~0.6"].win_rate == 0.5
    assert by["0.8~0.9"].n == 2 and by["0.8~0.9"].win_rate == 1.0
    assert by["0.9~1.0"].n == 1
    assert "0.6~0.7" not in by                                        # 빈 버킷 생략


def test_is_monotonic():
    inc = [BucketStat("a", 1, 0.01, 1), BucketStat("b", 1, 0.02, 1)]
    dec = [BucketStat("a", 1, 0.05, 1), BucketStat("b", 1, 0.01, 1)]
    assert is_monotonic(inc) is True and is_monotonic(dec) is False
