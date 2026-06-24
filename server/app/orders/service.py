"""주문 레이어 = 모드 게이트 + 하드 가드레일 + 멱등 + 감사.

안전 불변식:
  - **DRY_RUN(기본): executor 를 절대 호출하지 않는다 → 실주문 0 보장.**
  - 가드레일은 모드 무관 선검사. 통과해야만 LIVE 전송.
  - 같은 clientOrderId 재시도는 이전 결과를 반환(중복 전송 방지).
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Protocol

from app.orders.guardrails import GuardrailConfig, GuardrailContext, run_guardrails
from app.orders.models import OrderRequest, OrderResult, OrderStatus, TradingMode


class OrderExecutor(Protocol):
    """실제 토스 주문 전송(LIVE 전용). 토스 orderId(식별자)를 반환."""

    def place(self, order: OrderRequest) -> str: ...


class CallableExecutor:
    """주입된 함수로 주문을 전송하는 LIVE 실행기.

    place_fn 없이는 생성 불가 → '실수로 실주문' 경로가 코드에 떠다니지 않게 한다.
    실제 toss 주문 함수는 추후(클라이언트 구현 단계)에 주입한다.
    """

    def __init__(self, place_fn: Callable[[OrderRequest], str]):
        self._place_fn = place_fn

    def place(self, order: OrderRequest) -> str:
        return self._place_fn(order)


AuditSink = Callable[[OrderResult], None]


class OrderService:
    def __init__(
        self,
        mode: TradingMode = TradingMode.DRY_RUN,
        config: GuardrailConfig | None = None,
        executor: OrderExecutor | None = None,
        audit: AuditSink | None = None,
    ):
        self.mode = mode
        self.config = config or GuardrailConfig()
        self._executor = executor
        self._audit = audit
        self.kill_switch = False
        self._seen: dict[str, OrderResult] = {}   # 멱등 원장
        self.ledger: list[OrderResult] = []        # 확정 결과 전수

    # ── 킬스위치 (글로벌, 서비스 소유) ──
    def engage_kill_switch(self) -> None:
        self.kill_switch = True

    def release_kill_switch(self) -> None:
        self.kill_switch = False

    # ── 주문 제출 ──
    def submit(self, order: OrderRequest, ctx: GuardrailContext) -> OrderResult:
        # 1) 멱등: 같은 clientOrderId 는 재전송 없이 이전 결과 반환
        prior = self._seen.get(order.client_order_id)
        if prior is not None:
            dup = prior.model_copy(update={"status": OrderStatus.DUPLICATE})
            self._emit(dup)
            return dup

        # 2) 가드레일(모드 무관). 킬스위치는 서비스 소유 상태를 주입
        eff_ctx = dataclasses.replace(ctx, kill_switch=ctx.kill_switch or self.kill_switch)
        violations = run_guardrails(order, eff_ctx, self.config)
        if violations:
            reason = "; ".join(f"[{v.code}] {v.reason}" for v in violations)
            return self._finalize(self._result(order, OrderStatus.REJECTED, reason=reason))

        # 3) 모드 게이트
        if self.mode is TradingMode.DRY_RUN:
            # ★ executor 를 호출하지 않는다 — 의도된 주문만 기록
            return self._finalize(
                self._result(order, OrderStatus.DRY_RUN, reason="DRY_RUN: 미전송(의도된 주문 기록)")
            )

        # LIVE
        if self._executor is None:
            return self._finalize(
                self._result(order, OrderStatus.FAILED, reason="LIVE executor 미설정 — 전송 거부")
            )
        try:
            toss_id = self._executor.place(order)
            res = self._result(order, OrderStatus.SUBMITTED, toss_order_id=toss_id)
        except Exception as e:  # 전송 실패는 삼키고 결과로 기록(상위에서 재시도 판단)
            res = self._result(order, OrderStatus.FAILED, reason=f"전송 오류: {e}")
        return self._finalize(res)

    # ── 내부 ──
    def _result(self, order, status, reason=None, toss_order_id=None) -> OrderResult:
        return OrderResult(
            client_order_id=order.client_order_id,
            status=status,
            mode=self.mode,
            request=order,
            reason=reason,
            toss_order_id=toss_order_id,
        )

    def _finalize(self, res: OrderResult) -> OrderResult:
        self._seen[res.client_order_id] = res
        self.ledger.append(res)
        self._emit(res)
        return res

    def _emit(self, res: OrderResult) -> None:
        if self._audit is not None:
            self._audit(res)

    @property
    def intended_orders(self) -> list[OrderResult]:
        return [r for r in self.ledger if r.status is OrderStatus.DRY_RUN]

    @property
    def sent_orders(self) -> list[OrderResult]:
        return [r for r in self.ledger if r.status is OrderStatus.SUBMITTED]
