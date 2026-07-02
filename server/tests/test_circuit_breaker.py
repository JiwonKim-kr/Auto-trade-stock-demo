"""서킷브레이커 단위 테스트 — 래칭·히스테리시스·일일 리셋·고점(HWM)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.orders.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from app.orders.guardrails import KST

D1 = datetime(2026, 6, 23, 10, 0, tzinfo=KST)   # 화
D1_LATE = datetime(2026, 6, 23, 14, 0, tzinfo=KST)
D2 = datetime(2026, 6, 24, 10, 0, tzinfo=KST)   # 수(다음 거래일)


def cb() -> CircuitBreaker:
    return CircuitBreaker(CircuitBreakerConfig())   # 일일 5% · MDD 15% · rearm 8%


# ── 일일 손실 ─────────────────────────────────────────────────────────────────
def test_daily_loss_trips_and_latches_for_the_day():
    b = cb()
    assert b.assess(Decimal("1000"), Decimal("-0.06"), D1) is True   # -6% ≤ -5%
    assert "일일 손실" in b.reason
    # 같은 날 반등해도 그날은 유지(당일 재진입 금지)
    assert b.assess(Decimal("1000"), Decimal("-0.01"), D1_LATE) is True


def test_daily_halt_auto_resets_next_day():
    b = cb()
    b.assess(Decimal("1000"), Decimal("-0.06"), D1)
    assert b.assess(Decimal("1000"), Decimal("-0.01"), D2) is False   # 다음 거래일 리셋


def test_daily_loss_just_under_limit_does_not_trip():
    b = cb()
    assert b.assess(Decimal("1000"), Decimal("-0.049"), D1) is False


# ── 고점대비 낙폭(MDD) + 히스테리시스 ──────────────────────────────────────────
def test_drawdown_trips_at_limit():
    b = cb()
    b.assess(Decimal("1000"), Decimal("0"), D1)                       # 고점 1000
    assert b.assess(Decimal("800"), Decimal("0"), D1) is True         # -20% ≥ 15%
    assert b.drawdown == Decimal("0.2")
    assert "낙폭" in b.reason


def test_drawdown_latches_until_rearm():
    b = cb()
    b.assess(Decimal("1000"), Decimal("0"), D1)
    b.assess(Decimal("800"), Decimal("0"), D1)                        # 발동
    assert b.assess(Decimal("900"), Decimal("0"), D1) is True         # -10% > rearm 8% → 유지
    assert b.assess(Decimal("925"), Decimal("0"), D1) is False        # -7.5% ≤ 8% → 해제


def test_high_water_mark_tracks_peak():
    b = cb()
    b.assess(Decimal("1000"), Decimal("0"), D1)
    b.assess(Decimal("1200"), Decimal("0"), D1)                       # 신고점
    assert b.high_water_mark == Decimal("1200")
    assert b.assess(Decimal("1000"), Decimal("0"), D1) is True        # 1200 대비 -16.7% ≥ 15%


def test_unknown_equity_does_not_reset_drawdown_halt():
    b = cb()
    b.assess(Decimal("1000"), Decimal("0"), D1)
    b.assess(Decimal("800"), Decimal("0"), D1)                        # 발동
    # equity 를 모르면(None) 낙폭 판정을 건너뜀 → 허위 해제 없음
    assert b.assess(None, Decimal("0"), D1) is True


def test_clean_state_not_tripped():
    b = cb()
    assert b.assess(Decimal("1000"), Decimal("0.02"), D1) is False
    assert b.reason == ""
    assert b.snapshot()["tripped"] is False


# ── 영속화 직렬화 (재시작 생존) ────────────────────────────────────────────────
def test_dump_restore_preserves_latches():
    b = cb()
    b.assess(Decimal("1000"), Decimal("-0.06"), D1)           # 일일 발동
    b.assess(Decimal("800"), Decimal("-0.06"), D1)            # 낙폭 발동(HWM 1000)
    state = b.dump_state()

    b2 = cb()                                                  # 재시작 시뮬레이션
    b2.restore_state(state)
    assert b2.tripped is True                                  # 래치 생존
    assert b2.high_water_mark == Decimal("1000")
    # 복원 후에도 히스테리시스 이어짐: 925(-7.5%) 회복 → 해제, 일일은 다음날 리셋
    assert b2.assess(Decimal("925"), Decimal("0"), D2) is False


def test_dump_restore_empty_state_noop():
    b = cb()
    b.restore_state({})                                        # 빈 상태(첫 기동) 안전
    assert b.tripped is False and b.high_water_mark is None
