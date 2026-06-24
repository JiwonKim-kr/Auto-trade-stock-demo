"""토스 Open API 비동기 클라이언트 — 인사이트 §2 함정을 코드 불변식으로 박는다.

  - 리소스 경로 prefix **/api/v1** (토큰만 /oauth2/token, prefix 없음).
  - 응답 **{ "result": ... } 언래핑**.
  - 계좌계열은 헤더 **X-Tossinvest-Account = accountSeq(정수)** 주입(계좌번호 아님).
  - 토큰: OAuth2 client_credentials, **인메모리 캐시 + 만료 전 갱신**.
  - 4xx 는 구조화 에러(TossAPIError)로, 401 은 토큰 재발급 후 1회 재시도, 5xx/네트워크는 백오프 재시도.
  - 필수 파라미터 가드: prices/stocks 는 symbols, buying-power 는 currency.

⚠️ 주문 전송(POST /api/v1/orders)은 여기 없다 — LIVE executor 연결 단계에서 추가(실자금 안전).
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.toss.models import Account, BuyingPower, Candle, CandleSeries, Holdings, Price, Stock

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"
API_PREFIX = "/api/v1"
TOKEN_PATH = "/oauth2/token"          # ⚠️ prefix 없음
TOKEN_REFRESH_MARGIN_S = 120          # 만료 N초 전 선갱신
# 재시도 대상 상태코드. 429 = 레이트 리밋(BASIC tier 실측, 2026-06): 빠른 연속 호출 시 발생.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 10.0              # Retry-After 존중하되 상한


class TossAPIError(Exception):
    """토스 비2xx 응답의 구조화 표현 ({error:{code,message,requestId}} / {code,message,field} 모두 수용)."""

    def __init__(self, status, code=None, message=None, field=None, request_id=None, raw=None):
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.request_id = request_id
        self.raw = raw
        suffix = f" (field={field})" if field else ""
        super().__init__(f"Toss API {status} [{code}] {message}{suffix}")


@dataclass
class TossConfig:
    client_id: str
    client_secret: str
    base_url: str = DEFAULT_BASE_URL
    account_seq: int | None = None    # 미지정 시 /accounts 로 자동 해석
    timeout: float = 15.0
    max_retries: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TossConfig":
        env = env if env is not None else os.environ
        cid, sec = env.get("TOSS_CLIENT_ID"), env.get("TOSS_CLIENT_SECRET")
        if not cid or not sec:
            raise RuntimeError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 미설정")
        seq = env.get("TOSS_ACCOUNT_SEQ")
        return cls(client_id=cid, client_secret=sec,
                   account_seq=int(seq) if seq else None)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def _unwrap(body: Any) -> Any:
    """함정 2: 최상위 {result} 를 벗긴다."""
    if isinstance(body, dict) and "result" in body:
        return body["result"]
    return body


def _parse_error(status: int, body: Any) -> TossAPIError:
    code = message = field = request_id = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):           # {error:{code,message,requestId[,data.field]}}
            code, message = err.get("code"), err.get("message")
            field = err.get("field") or (err.get("data") or {}).get("field")
            request_id = err.get("requestId")
        elif isinstance(err, str):          # OAuth2: {error, error_description}
            code, message = err, body.get("error_description")
        else:                               # {code,message,field}
            code, message = body.get("code"), body.get("message")
            field, request_id = body.get("field"), body.get("requestId")
    return TossAPIError(status, code, message, field, request_id, raw=body)


def _csv(symbols: Iterable[str] | str) -> str:
    return symbols if isinstance(symbols, str) else ",".join(symbols)


class TossClient:
    def __init__(self, config: TossConfig, http: httpx.AsyncClient | None = None):
        self._cfg = config
        self._http = http or httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout)
        self._owns_http = http is None
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._account_seq: int | None = config.account_seq
        self._token_lock = asyncio.Lock()

    async def __aenter__(self) -> "TossClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ── 토큰 ──────────────────────────────────────────────────────────────────
    def _token_valid(self) -> bool:
        return bool(self._token) and time.time() < self._token_exp - TOKEN_REFRESH_MARGIN_S

    async def _ensure_token(self) -> str:
        if self._token_valid():
            return self._token  # type: ignore[return-value]
        async with self._token_lock:
            if self._token_valid():        # 락 경합 중 갱신됐을 수 있음
                return self._token  # type: ignore[return-value]
            await self._fetch_token()
            return self._token  # type: ignore[return-value]

    async def _fetch_token(self) -> None:
        basic = base64.b64encode(
            f"{self._cfg.client_id}:{self._cfg.client_secret}".encode()
        ).decode()
        resp = await self._http.post(
            TOKEN_PATH,
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        body = _safe_json(resp)
        if resp.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise _parse_error(resp.status_code, body)
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 0))

    # ── 계좌 식별자 ────────────────────────────────────────────────────────────
    async def _ensure_account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        accounts = await self.get_accounts()
        if not accounts:
            raise TossAPIError(0, code="no-account", message="조회 가능한 계좌가 없습니다")
        self._account_seq = accounts[0].account_seq
        return self._account_seq

    # ── 코어 요청 ──────────────────────────────────────────────────────────────
    async def _request(self, method: str, path: str, *, account: bool = False,
                       params: dict | None = None, json: dict | None = None,
                       _retried_auth: bool = False) -> Any:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if account:
            headers["X-Tossinvest-Account"] = str(await self._ensure_account_seq())

        resp = await self._send_with_retry(method, API_PREFIX + path,
                                           headers=headers, params=params, json=json)

        if resp.status_code == 401 and not _retried_auth:
            self._token = None            # 강제 재발급 후 1회 재시도
            return await self._request(method, path, account=account, params=params,
                                       json=json, _retried_auth=True)

        body = _safe_json(resp)
        if resp.status_code >= 400:
            raise _parse_error(resp.status_code, body)
        return _unwrap(body)

    async def _send_with_retry(self, method: str, url: str, **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                resp = await self._http.request(method, url, **kw)
            except httpx.TransportError as e:       # 네트워크/타임아웃 → 재시도
                last_exc = e
                if attempt < self._cfg.max_retries:
                    await asyncio.sleep(0.3 * (2**attempt))
                    continue
                raise
            if resp.status_code in RETRYABLE_STATUS and attempt < self._cfg.max_retries:
                await asyncio.sleep(self._retry_delay(resp, attempt))
                continue
            return resp
        raise last_exc  # pragma: no cover

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        """Retry-After(초) 존중, 없으면 지수 백오프. 429 는 더 보수적으로."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        base = 0.5 if resp.status_code == 429 else 0.2
        return base * (2**attempt)

    # ── 조회 메서드 ────────────────────────────────────────────────────────────
    async def get_accounts(self) -> list[Account]:
        data = await self._request("GET", "/accounts")           # 헤더 불필요
        return [Account.model_validate(a) for a in data]

    async def get_holdings(self) -> Holdings:
        data = await self._request("GET", "/holdings", account=True)
        return Holdings.model_validate(data)

    async def get_buying_power(self, currency: str = "KRW") -> BuyingPower:
        if not currency:
            raise ValueError("currency 는 필수입니다 (없으면 400)")
        data = await self._request("GET", "/buying-power", account=True,
                                   params={"currency": currency})
        return BuyingPower.model_validate(data)

    async def get_prices(self, symbols: Iterable[str] | str) -> list[Price]:
        if not symbols:
            raise ValueError("symbols 는 필수입니다 (전체 목록 엔드포인트 없음)")
        data = await self._request("GET", "/prices", params={"symbols": _csv(symbols)})
        return [Price.model_validate(p) for p in data]

    async def get_stocks(self, symbols: Iterable[str] | str) -> list[Stock]:
        if not symbols:
            raise ValueError("symbols 는 필수입니다")
        data = await self._request("GET", "/stocks", params={"symbols": _csv(symbols)})
        return [Stock.model_validate(s) for s in data]

    async def get_stock_warnings(self, symbol: str) -> list[dict]:
        # 종목별 경고. 채워진 형태 미확정 → 원시 dict 리스트로 반환.
        return await self._request("GET", f"/stocks/{symbol}/warnings")

    async def get_candles(self, symbol: str, interval: str = "1d") -> list[Candle]:
        """과거 봉. interval 필수(없으면 400 field=interval). 결과는 **과거→최신** 정렬로 정규화."""
        if not symbol:
            raise ValueError("symbol 은 필수입니다")
        if not interval:
            raise ValueError("interval 은 필수입니다")
        data = await self._request(
            "GET", "/candles", params={"symbol": symbol, "interval": interval}
        )
        series = CandleSeries.model_validate(data)
        return sorted(series.candles, key=lambda c: c.timestamp)
