"""FastAPI 앱 — 거래 서버(두뇌).

실행(로컬): uvicorn app.main:app --reload   (server/ 에서)
거래 모드는 DRY_RUN 기본 + LIVE 다중 확인(core.config.load_trading_mode).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import load_trading_mode
from app.core.settings import get_settings
from app.orders.guardrails import GuardrailConfig
from app.orders.service import OrderService
from app.toss.client import TossClient, TossConfig

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    mode, warnings = load_trading_mode()
    for w in warnings:
        logger.warning(w)
    logger.info("TRADING_MODE=%s", mode.value)

    app.state.settings = settings
    app.state.trading_mode = mode
    app.state.order_service = OrderService(
        mode=mode,
        config=GuardrailConfig(
            per_order_max_krw=settings.per_order_max_krw,
            daily_buy_cap_krw=settings.daily_buy_cap_krw,
            max_positions=settings.max_positions,
            per_symbol_max_weight=settings.per_symbol_max_weight,
            enforce_market_hours=settings.enforce_market_hours,
        ),
    )

    app.state.toss_client = None
    if settings.toss_client_id and settings.toss_client_secret:
        app.state.toss_client = TossClient(
            TossConfig(
                client_id=settings.toss_client_id,
                client_secret=settings.toss_client_secret,
                account_seq=settings.toss_account_seq,
            )
        )
    else:
        logger.warning("토스 자격증명 미설정 — 토스 연동 엔드포인트는 503 반환")

    if settings.api_key == "dev-local-key":
        logger.warning("API_KEY 가 기본값입니다 — 운영 전 반드시 변경(Secret Manager)")

    try:
        yield
    finally:
        if app.state.toss_client is not None:
            await app.state.toss_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="토스 AI 자동매매 서버", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
