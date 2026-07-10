"""FastAPI 앱 — 거래 서버(두뇌).

실행(로컬): uvicorn app.main:app --reload   (server/ 에서)
거래 모드는 DRY_RUN 기본 + LIVE 다중 확인(core.config.load_trading_mode).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.api.tick import tick_loop
from app.core.calendar import load_holidays
from app.core.config import load_trading_mode
from app.core.logging_setup import setup_json_logging
from app.core.notify import AlertGate, NullNotifier, TelegramNotifier
from app.core.settings import get_settings
from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.orders.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from app.orders.guardrails import GuardrailConfig
from app.orders.models import TradingMode
from app.orders.service import OrderService
from app.toss.client import TossClient, TossConfig

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 하드닝(§3.7): 운영에서 기본 API 키는 문서화된 값 = 사실상 무인증 → 강등이 아니라 기동 거부
    # (조용한 노출이 더 위험 — §1.1 LIVE-DB 강등과 달리 안전한 폴백이 존재하지 않는다)
    if settings.app_env == "production" and settings.api_key == "dev-local-key":
        raise RuntimeError(
            "APP_ENV=production 인데 API_KEY 가 기본값(dev-local-key) — 기동 거부. "
            "Secret Manager 등으로 API_KEY 를 주입하라 (IMPLEMENTATION-PLAN §3.7)")
    mode, warnings = load_trading_mode()
    for w in warnings:
        logger.warning(w)
    logger.info("TRADING_MODE=%s", mode.value)

    app.state.settings = settings
    app.state.trading_mode = mode
    app.state.holidays = load_holidays(settings.krx_holidays_path)   # KRX 공휴일(주말 외)
    app.state.order_service = OrderService(
        mode=mode,
        config=GuardrailConfig(
            per_order_max_krw=settings.per_order_max_krw,
            daily_buy_cap_krw=settings.daily_buy_cap_krw,
            max_positions=settings.max_positions,
            per_symbol_max_weight=settings.per_symbol_max_weight,
            enforce_market_hours=settings.enforce_market_hours,
            holidays=app.state.holidays,
        ),
        circuit_breaker=CircuitBreaker(
            CircuitBreakerConfig(
                daily_loss_limit=settings.daily_loss_limit,
                max_drawdown_limit=settings.max_drawdown_limit,
                rearm_drawdown=settings.drawdown_rearm,
            )
        ),
    )

    # DB 영속화(선택): 틱/결정/주문 기록 + 킬스위치·서킷브레이커 상태 재시작 생존
    app.state.repo = None
    app.state.db_engine = None
    if settings.database_url:
        engine = make_engine(settings.database_url)
        await init_db(engine)
        repo = Repository(make_sessionmaker(engine))
        app.state.repo, app.state.db_engine = repo, engine
        state = await repo.load_engine_state()
        if state is not None:
            kill_switch, breaker = state
            if kill_switch:
                app.state.order_service.engage_kill_switch()
                logger.warning("영속화된 킬스위치 ON 복원 — 해제 전까지 주문 차단")
            if breaker:
                app.state.order_service.circuit_breaker.restore_state(breaker)
        logger.info("DB 영속화 활성")
    else:
        logger.warning("DATABASE_URL 미설정 — 인메모리(재시작 시 원장/엔진 상태 소실)")

    # 안전 강제(P0): LIVE 는 DB 필수 — 일일 한도 교차-틱 누적·리컨실·멱등 2차방어·상태 생존이
    # 전부 DB 전제다. 없으면 일일 한도가 틱마다 리셋되는 등 실자금 방어가 무너진다 → 강제 강등.
    if mode is TradingMode.LIVE and app.state.repo is None:
        mode = TradingMode.DRY_RUN
        app.state.order_service.mode = mode
        app.state.trading_mode = mode
        logger.critical("LIVE 요청됐으나 DATABASE_URL 미설정 — DRY_RUN 강등 "
                        "(일일한도 누적·리컨실·멱등 2차방어가 DB 전제)")

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

    # 알림 채널(미설정 = 무음) + 반복 억제 게이트
    if settings.notify_telegram_bot_token and settings.notify_telegram_chat_id:
        app.state.notifier = TelegramNotifier(settings.notify_telegram_bot_token,
                                              settings.notify_telegram_chat_id)
        logger.info("텔레그램 알림 활성")
    else:
        app.state.notifier = NullNotifier()
    app.state.alert_gate = AlertGate()

    # 틱 직렬화 락(+내장 루프). 운영(Cloud Scheduler)은 interval=0 — 루프 없이 락만 사용
    app.state.tick_lock = asyncio.Lock()
    loop_task = None
    if settings.tick_interval_sec > 0:
        loop_task = asyncio.create_task(tick_loop(app))

    try:
        yield
    finally:
        if loop_task is not None:
            loop_task.cancel()
        if app.state.toss_client is not None:
            await app.state.toss_client.aclose()
        if isinstance(app.state.notifier, TelegramNotifier):
            await app.state.notifier.aclose()
        if app.state.db_engine is not None:
            await app.state.db_engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.log_format == "json":     # Cloud Logging 용 구조화 로깅(§3.8)
        setup_json_logging()
    # 하드닝(§3.7): 운영에선 /docs·/openapi.json 무인증 노출 차단(라우트 맵·헤더명 정찰 방지)
    production = settings.app_env == "production"
    app = FastAPI(title="토스 AI 자동매매 서버", version="0.1.0", lifespan=lifespan,
                  docs_url=None if production else "/docs",
                  redoc_url=None if production else "/redoc",
                  openapi_url=None if production else "/openapi.json")
    app.include_router(router)
    return app


app = create_app()
