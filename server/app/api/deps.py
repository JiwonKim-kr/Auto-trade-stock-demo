"""API 의존성 — 인증(API 키 · OIDC) + 서비스/클라이언트 접근."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.core.settings import Settings, get_settings
from app.orders.service import OrderService
from app.toss.client import TossClient

try:   # OIDC(§3.3)는 운영(Scheduler) 전용 — 미설치 환경(구버전 로컬)은 API 키 경로만
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
except ImportError:                                    # pragma: no cover
    google_requests = id_token = None                  # type: ignore[assignment]


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """데스크톱 ↔ 서버 인증. 상수시간 비교로 X-API-Key 검증."""
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효한 X-API-Key 가 필요합니다")


async def require_tick_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """/internal/* 인증 — Scheduler(OIDC Bearer) 또는 API 키 이중 경로(§3.3).

    OIDC 는 OIDC_AUDIENCE 설정 시에만 시도하며, 검증 실패는 위조·만료·구글 공개키
    조회 실패(네트워크)를 가리지 않고 전부 401(fail-closed — 500 으로 새지 않게).
    Bearer 를 제시했다면 API 키로 폴백하지 않는다(잘못된 토큰이 조용히 통과하는 것 방지).
    """
    if authorization and authorization.startswith("Bearer ") and settings.oidc_audience:
        if id_token is None:                           # pragma: no cover
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC 미지원(google-auth 미설치)")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = await run_in_threadpool(          # verify 는 동기(공개키 HTTP 조회 포함)
                id_token.verify_oauth2_token, token,
                google_requests.Request(), settings.oidc_audience)
        except Exception:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC 토큰 검증 실패") from None
        if claims.get("email") == settings.scheduler_sa_email and claims.get("email_verified"):
            return
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "OIDC 서비스 계정 불일치")
    # 폴백: API 키(로컬 내장 루프·수동 호출 — 기존 경로 보존)
    if x_api_key and secrets.compare_digest(x_api_key, settings.api_key):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증 실패 — OIDC Bearer 또는 X-API-Key 필요")


def get_order_service(request: Request) -> OrderService:
    return request.app.state.order_service


def get_toss_client(request: Request) -> TossClient:
    client = request.app.state.toss_client
    if client is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "토스 자격증명 미설정 — 서버에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 필요",
        )
    return client
