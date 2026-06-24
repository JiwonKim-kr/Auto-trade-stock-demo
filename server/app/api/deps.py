"""API 의존성 — 인증(API 키) + 서비스/클라이언트 접근."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.settings import Settings, get_settings
from app.orders.service import OrderService
from app.toss.client import TossClient


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """데스크톱 ↔ 서버 인증. 상수시간 비교로 X-API-Key 검증."""
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "유효한 X-API-Key 가 필요합니다")


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
