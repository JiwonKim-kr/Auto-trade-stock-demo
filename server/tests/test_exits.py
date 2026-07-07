"""결정적 청산(손절·타임스톱) 판정 테스트 — LLM 우회 하드 룰의 경계 조건."""

from __future__ import annotations

from decimal import Decimal

from app.engine.exits import ExitConfig, evaluate_exits
from app.engine.paper import PaperPosition

D = Decimal


def pos(qty="10", avg="10000") -> PaperPosition:
    return PaperPosition(quantity=D(qty), avg_cost=D(avg))


def test_stop_loss_triggers_at_boundary():
    # 정확히 -8% = 발동(≤)
    forced = evaluate_exits({"A": pos(avg="10000")}, {"A": D("9200")}, {})
    assert len(forced) == 1 and "손절" in forced[0].reason and forced[0].symbol == "A"


def test_stop_loss_not_triggered_above_boundary():
    forced = evaluate_exits({"A": pos(avg="10000")}, {"A": D("9201")}, {})
    assert forced == []


def test_missing_mark_defers_stop_loss():
    # 시세 없음 → 손절 판정 보류(허위 청산 방지). 타임스톱은 가격 무관이라 적용됨
    assert evaluate_exits({"A": pos()}, {}, {}) == []
    forced = evaluate_exits({"A": pos()}, {}, {"A": 25})
    assert len(forced) == 1 and "타임스톱" in forced[0].reason


def test_time_stop_boundary():
    assert evaluate_exits({"A": pos()}, {"A": D("10000")}, {"A": 20}) == []       # 20 == 20 → 유지
    forced = evaluate_exits({"A": pos()}, {"A": D("10000")}, {"A": 21})           # 21 > 20 → 청산
    assert len(forced) == 1 and "21거래일" in forced[0].reason


def test_stop_loss_takes_precedence_over_time_stop():
    forced = evaluate_exits({"A": pos(avg="10000")}, {"A": D("8000")}, {"A": 99})
    assert len(forced) == 1 and "손절" in forced[0].reason


def test_unknown_opened_at_skips_time_stop():
    # days_held 에 없는 심볼(opened_at 미기록 — 하위호환) → 타임스톱 미적용
    assert evaluate_exits({"A": pos()}, {"A": D("10000")}, {}) == []


def test_disabled_returns_empty():
    forced = evaluate_exits({"A": pos(avg="10000")}, {"A": D("5000")}, {"A": 99},
                            ExitConfig(enabled=False))
    assert forced == []


def test_custom_thresholds():
    cfg = ExitConfig(stop_loss_rate=D("0.03"), time_stop_days=5)
    assert evaluate_exits({"A": pos(avg="10000")}, {"A": D("9700")}, {}, cfg)     # -3% 발동
    assert evaluate_exits({"B": pos()}, {"B": D("10000")}, {"B": 6}, cfg)          # 6 > 5 발동


def test_multiple_positions_sorted():
    forced = evaluate_exits(
        {"B": pos(avg="10000"), "A": pos(avg="10000")},
        {"A": D("9000"), "B": D("9000")}, {})
    assert [f.symbol for f in forced] == ["A", "B"]           # 결정적 순서(정렬)
