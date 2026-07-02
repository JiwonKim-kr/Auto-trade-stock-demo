"""리컨실 — 시스템이 아는 포지션(DB 스냅샷+주문) ↔ 브로커 실제(토스 holdings) 대조 (TECH-STACK §7).

목적: 시스템의 '믿음'과 계좌의 '현실'이 어긋나면(수동 매매·미인지 체결·입출고 등) 그 위에서의
자동 거래는 위험하다 → **불일치를 감지해 알리고, LIVE 에선 거래를 중단**시킨다.

기대 수량 모델:  expected[symbol] = 직전 스냅샷 수량 + 그 이후 SUBMITTED 주문 순증감(매수+·매도−)
  - DRY_RUN: 전송 주문이 없으므로 순증감=0 → 스냅샷 대비 **모든 변화 = 외부 변화**로 감지.
  - LIVE: 전송(SUBMITTED) 기준 근사 — 체결 조회 연동 전이라 **미체결/부분체결도 불일치로 뜬다**
    (보수적 오탐: 조용한 표류보다 잘못된 경보가 낫다). 체결 API 연동 시 정밀화. (TODO)

동작(모드별, 라우트가 집행):
  - DRY_RUN: 감사 기록 + 응답 노출만(수동 매매가 정상인 관찰 단계 — halt 는 소음).
  - LIVE:    킬스위치 자동 발동(거래 중단) + 감사. 해제는 운영자 수동(원인 확인 후).

이 모듈은 **순수 비교만** 한다(DB/토스 조회는 경계=라우트 책임 — 파이프라인 주입 철학과 동일).
비교 대상은 수량만: 평단가·평가액 표류(배당·수수료 반영 등)는 halt 사유가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.toss.models import Holdings


class DiscrepancyKind(str, Enum):
    NEW_SYMBOL = "NEW_SYMBOL"              # 시스템 모르게 나타난 종목
    MISSING_SYMBOL = "MISSING_SYMBOL"      # 시스템 모르게 사라진 종목
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"


@dataclass(frozen=True)
class PositionSnapshot:
    """스냅샷 1종목분(저장용). 수량 외 필드는 감사·표시용."""

    symbol: str
    quantity: Decimal
    avg_price: Decimal | None = None
    currency: str | None = None


@dataclass
class Discrepancy:
    kind: DiscrepancyKind
    symbol: str
    expected: Decimal
    actual: Decimal
    detail: str

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "symbol": self.symbol,
                "expected": str(self.expected), "actual": str(self.actual),
                "detail": self.detail}


@dataclass
class ReconcileReport:
    baseline: bool = False                              # 직전 스냅샷 없음 → 이번이 기준선
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def status(self) -> str:
        if self.baseline:
            return "BASELINE"
        return "OK" if self.ok else "MISMATCH"

    def as_dict(self) -> dict:
        return {"status": self.status,
                "discrepancies": [d.as_dict() for d in self.discrepancies]}


def snapshot_from_holdings(holdings: Holdings) -> list[PositionSnapshot]:
    return [PositionSnapshot(symbol=i.symbol, quantity=i.quantity,
                             avg_price=i.average_purchase_price, currency=i.currency)
            for i in holdings.items]


def reconcile(
    previous: dict[str, Decimal] | None,
    current: dict[str, Decimal],
    submitted_delta: dict[str, Decimal] | None = None,
) -> ReconcileReport:
    """직전 스냅샷(previous) + 전송 주문 순증감 → 기대 수량 vs 실제(current) 대조.

    previous 가 None(첫 실행)이면 기준선 생성으로 처리(불일치 아님).
    """
    if previous is None:
        return ReconcileReport(baseline=True)
    delta = submitted_delta or {}

    report = ReconcileReport()
    for symbol in sorted(set(previous) | set(current) | set(delta)):
        expected = previous.get(symbol, Decimal(0)) + delta.get(symbol, Decimal(0))
        actual = current.get(symbol, Decimal(0))
        if actual == expected:
            continue
        if symbol not in previous and symbol not in delta:
            kind, detail = DiscrepancyKind.NEW_SYMBOL, "시스템 미인지 신규 보유(외부 매수/입고?)"
        elif symbol not in current and expected != 0:
            kind, detail = DiscrepancyKind.MISSING_SYMBOL, "시스템 미인지 보유 소멸(외부 매도/출고?)"
        else:
            kind = DiscrepancyKind.QUANTITY_MISMATCH
            detail = (f"수량 불일치(전송분 반영 기대 {expected})"
                      + (f" · 전송 순증감 {delta[symbol]}" if symbol in delta else ""))
        report.discrepancies.append(
            Discrepancy(kind=kind, symbol=symbol, expected=expected, actual=actual, detail=detail))
    return report
