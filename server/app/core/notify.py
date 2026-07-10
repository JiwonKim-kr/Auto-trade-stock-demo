"""알림 채널 — 안전 이벤트(서킷브레이커·리컨실·틱 실패·킬스위치)를 운영자에게 push (PLAN §3.5).

설계 원칙:
  - **알림 실패가 틱을 죽이면 안 된다** — 전송 오류는 삼키고 warning 로그만.
  - **스팸 방지가 설계의 핵심** — 상태 이벤트는 전이(transition)만, 반복 이벤트는 AlertGate 로
    같은 키를 시간창(기본 60분) 내 1회로 억제.
  - 비밀·계좌번호를 메시지에 절대 포함하지 않는다.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

logger = logging.getLogger("app.notify")


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class NullNotifier:
    """미설정 시 — 무음."""

    async def send(self, text: str) -> None:
        return None


class TelegramNotifier:
    """텔레그램 봇 sendMessage. timeout 5s, 실패는 로그만(틱 보호)."""

    def __init__(self, bot_token: str, chat_id: str, http: httpx.AsyncClient | None = None):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._http = http or httpx.AsyncClient(timeout=5.0)

    async def send(self, text: str) -> None:
        try:
            resp = await self._http.post(self._url, json={"chat_id": self._chat_id, "text": text})
            if resp.status_code != 200:
                logger.warning("텔레그램 알림 실패 status=%s", resp.status_code)
        except Exception as e:  # 네트워크/타임아웃 — 알림은 best-effort
            logger.warning("텔레그램 알림 오류: %s", e)

    async def aclose(self) -> None:
        await self._http.aclose()


class AlertGate:
    """반복 알림 억제 — 같은 키는 window_sec 내 1회만 허용(인메모리, 재시작 시 리셋 허용)."""

    def __init__(self):
        self._last_sent: dict[str, float] = {}

    def allow(self, key: str, window_sec: float = 3600.0) -> bool:
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < window_sec:
            return False
        self._last_sent[key] = now
        return True
