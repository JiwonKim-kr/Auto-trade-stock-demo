"""거래 모드 결정 — DRY_RUN 기본 + LIVE 다중 확인 (인사이트 §6 안전).

LIVE 가 되려면 **둘 다** 필요:
  - TRADING_MODE=LIVE
  - I_UNDERSTAND_LIVE_REAL_MONEY=YES   (실자금 인지 2차 확인)
조건 미충족 시 DRY_RUN 으로 강등하고 경고를 반환한다(안전 우선, 절대 양보 없음).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from app.orders.models import TradingMode

LIVE_CONFIRM_ENV = "I_UNDERSTAND_LIVE_REAL_MONEY"
LIVE_CONFIRM_VALUE = "YES"


def load_trading_mode(env: Mapping[str, str] | None = None) -> tuple[TradingMode, list[str]]:
    """(mode, warnings) 반환. 호출 측은 warnings 를 반드시 로깅/표시할 것."""
    env = env if env is not None else os.environ
    warnings: list[str] = []

    raw = (env.get("TRADING_MODE") or "DRY_RUN").strip().upper()
    if raw != "LIVE":
        return TradingMode.DRY_RUN, warnings

    confirm = (env.get(LIVE_CONFIRM_ENV) or "").strip().upper()
    if confirm != LIVE_CONFIRM_VALUE:
        warnings.append(
            f"TRADING_MODE=LIVE 이지만 {LIVE_CONFIRM_ENV}={LIVE_CONFIRM_VALUE} 확인이 없어 "
            "DRY_RUN 으로 강등합니다(실자금 안전장치)."
        )
        return TradingMode.DRY_RUN, warnings

    warnings.append("⚠️ LIVE 모드 — 실자금 주문이 전송됩니다. 소액 1주부터 검증하세요.")
    return TradingMode.LIVE, warnings
