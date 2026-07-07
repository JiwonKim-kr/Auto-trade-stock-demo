"""결정적 청산 규칙 — 손절·타임스톱 (IMPLEMENTATION-PLAN §1.2, LLM 우회 하드 룰).

진입은 다층 방어(스크리너→게이트→레짐→가드레일)인데 청산이 LLM 재량뿐이면, 단일 포지션의
깊은 손실이 LLM 의 HOLD 관성에 방치될 수 있다 → **LLM 판단을 거치지 않는 강제 청산**:

  - 손절: 취득단가 대비 마킹가 손실률 ≥ stop_loss_rate(기본 8%) → 전량 청산.
    마킹가가 없으면(시세 조회 실패) **판정 보류** — 허위 청산 방지, 다음 틱 재시도.
  - 타임스톱: 보유 **거래일**(달력일 아님 — 틱이 돌았던 날 수) > time_stop_days(기본 20) → 청산.
    "그 아이디어에 돈이 묶인 시간"을 재는 것이므로 추가매수에도 최초 진입 기준(opened_at 유지).
    opened_at 미기록(과거 데이터)이면 미적용(하위호환).
  - 손절이 타임스톱보다 우선(둘 다 해당 시 사유는 손절).

강제 청산은 pipeline 에서 해당 심볼의 LLM 판단을 건너뛰고 SELL 을 생성한다(비용 절약 +
LLM 이 HOLD 로 뒤집는 것 원천 차단). 이 모듈은 순수 판정만 — 거래일 카운트·마킹은 경계(tick.py)가 주입.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel

from app.engine.paper import PaperPosition


class ExitConfig(BaseModel):
    stop_loss_rate: Decimal = Decimal("0.08")   # 취득단가 대비 손실률(양수 크기) ≥ 이 값 → 손절
    time_stop_days: int = 20                     # 보유 거래일 > 이 값 → 타임스톱
    enabled: bool = True


@dataclass
class ForcedExit:
    symbol: str
    reason: str

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "reason": self.reason}


def evaluate_exits(
    positions: dict[str, PaperPosition],
    marks: dict[str, Decimal],
    days_held: dict[str, int],
    cfg: ExitConfig | None = None,
) -> list[ForcedExit]:
    """포지션별 강제 청산 판정. marks 에 없는 심볼은 손절 판정 보류(타임스톱은 가격 무관 적용)."""
    cfg = cfg or ExitConfig()
    if not cfg.enabled:
        return []
    forced: list[ForcedExit] = []
    for symbol in sorted(positions):
        pos = positions[symbol]
        if pos.quantity <= 0 or pos.avg_cost <= 0:
            continue
        mark = marks.get(symbol)
        if mark is not None:
            loss = (mark - pos.avg_cost) / pos.avg_cost
            if loss <= -cfg.stop_loss_rate:
                forced.append(ForcedExit(
                    symbol, f"손절 {loss * 100:.1f}% ≤ -{cfg.stop_loss_rate * 100:.1f}%"))
                continue                                       # 손절 우선 — 타임스톱 중복 불필요
        held = days_held.get(symbol)
        if held is not None and held > cfg.time_stop_days:
            forced.append(ForcedExit(
                symbol, f"타임스톱 {held}거래일 > {cfg.time_stop_days}"))
    return forced
