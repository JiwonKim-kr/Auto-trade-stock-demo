"""토스 클라이언트 테스트 (respx 로 httpx mock).

실응답 픽스처(tests/fixtures/*.json)를 mock 응답으로 사용 → 클라이언트+모델 end-to-end 확인.
검증: 토큰 캐시 · {result} 언래핑 · 계좌헤더(accountSeq) 주입 · 필수 파라미터 · 구조화 에러
· 401 재발급 재시도 · 계좌 자동 해석 · 5xx 재시도.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.toss.client import TossAPIError, TossClient, TossConfig, _csv

BASE = "https://openapi.tossinvest.com"
FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def token_resp(expires_in: int = 86399) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": "tok-abc", "token_type": "Bearer", "expires_in": expires_in}
    )


@pytest.fixture
def cfg() -> TossConfig:
    return TossConfig(client_id="id", client_secret="sec", base_url=BASE, account_seq=1)


# ── 순수 단위 ─────────────────────────────────────────────────────────────────
def test_csv_join():
    assert _csv(["A", "B"]) == "A,B"
    assert _csv("A,B") == "A,B"


# ── 토큰/언래핑 ──────────────────────────────────────────────────────────────
@respx.mock
async def test_token_cached_and_accounts_unwrapped(cfg):
    tok = respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    acc = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=fixture("accounts.json"))
    )
    async with TossClient(cfg) as c:
        a1 = await c.get_accounts()
        a2 = await c.get_accounts()
    assert a1[0].account_seq == 1 and a1[0].account_type == "BROKERAGE"
    assert a2[0].account_seq == 1
    assert tok.call_count == 1          # 토큰 캐시 → 1회
    assert acc.call_count == 2


# ── 계좌 헤더 주입 ────────────────────────────────────────────────────────────
@respx.mock
async def test_holdings_injects_account_seq_header(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json=fixture("holdings.json"))
    )
    async with TossClient(cfg) as c:
        h = await c.get_holdings()
    req = route.calls.last.request
    assert req.headers.get("X-Tossinvest-Account") == "1"      # accountSeq(정수) 문자열
    assert req.headers.get("Authorization") == "Bearer tok-abc"
    # 모델 매핑까지 동작
    assert h.profit_loss.rate_percent == h.profit_loss.rate * 100
    assert h.items[1].currency == "USD" and h.items[1].cost.tax is None


@respx.mock
async def test_accounts_has_no_account_header(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=fixture("accounts.json"))
    )
    async with TossClient(cfg) as c:
        await c.get_accounts()
    assert "X-Tossinvest-Account" not in route.calls.last.request.headers


# ── 필수 파라미터 ─────────────────────────────────────────────────────────────
@respx.mock
async def test_prices_sends_symbols_param(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/prices").mock(
        return_value=httpx.Response(200, json=fixture("prices.json"))
    )
    async with TossClient(cfg) as c:
        prices = await c.get_prices(["005930"])
    assert route.calls.last.request.url.params["symbols"] == "005930"
    assert prices[0].last_price == Decimal("315500")
    assert prices[0].timestamp.tzinfo is not None


@respx.mock
async def test_buying_power_sends_currency_param(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/buying-power").mock(
        return_value=httpx.Response(200, json=fixture("buying_power.json"))
    )
    async with TossClient(cfg) as c:
        bp = await c.get_buying_power("KRW")
    assert route.calls.last.request.url.params["currency"] == "KRW"
    assert bp.cash_buying_power == Decimal("0")


async def test_empty_symbols_raises(cfg):
    async with TossClient(cfg) as c:
        with pytest.raises(ValueError):
            await c.get_prices([])


# ── 종목 플래그 ───────────────────────────────────────────────────────────────
@respx.mock
async def test_stocks_parse_risk_flags(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    respx.get(f"{BASE}/api/v1/stocks").mock(
        return_value=httpx.Response(200, json=fixture("stocks.json"))
    )
    async with TossClient(cfg) as c:
        stocks = await c.get_stocks(["005930"])
    s = stocks[0]
    assert s.is_common_share is True and s.leverage_factor is None
    assert s.korean_market_detail.liquidation_trading is False


# ── 에러 처리 ─────────────────────────────────────────────────────────────────
@respx.mock
async def test_account_not_found_raises_structured(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"requestId": "x", "code": "account-not-found",
                            "message": "해당 계좌번호를 찾을 수 없습니다."}},
        )
    )
    async with TossClient(cfg) as c:
        with pytest.raises(TossAPIError) as ei:
            await c.get_holdings()
    assert ei.value.status == 400
    assert ei.value.code == "account-not-found"
    assert ei.value.request_id == "x"


@respx.mock
async def test_token_failure_raises():
    cfg = TossConfig(client_id="bad", client_secret="bad", base_url=BASE)
    respx.post(f"{BASE}/oauth2/token").mock(
        return_value=httpx.Response(
            401, json={"error": "invalid_client", "error_description": "Client authentication failed"}
        )
    )
    async with TossClient(cfg) as c:
        with pytest.raises(TossAPIError) as ei:
            await c.get_accounts()
    assert ei.value.status == 401 and ei.value.code == "invalid_client"


# ── 401 재발급 · 5xx 재시도 · 계좌 자동 해석 ──────────────────────────────────
@respx.mock
async def test_401_refreshes_token_and_retries(cfg):
    tok = respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    acc = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(401, json={"error": {"code": "unauthorized", "message": "expired"}}),
            httpx.Response(200, json=fixture("accounts.json")),
        ]
    )
    async with TossClient(cfg) as c:
        accounts = await c.get_accounts()
    assert accounts[0].account_seq == 1
    assert tok.call_count == 2          # 초기 + 401 후 재발급
    assert acc.call_count == 2


@respx.mock
async def test_5xx_retried_then_success(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=fixture("accounts.json")),
        ]
    )
    async with TossClient(cfg) as c:
        accounts = await c.get_accounts()
    assert accounts[0].account_seq == 1
    assert route.call_count == 2


@respx.mock
async def test_429_rate_limit_retried_then_success(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/accounts").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"code": "rate-limit-exceeded",
                                                "message": "요청 한도를 초과했습니다."}}),
            httpx.Response(200, json=fixture("accounts.json")),
        ]
    )
    async with TossClient(cfg) as c:
        accounts = await c.get_accounts()
    assert accounts[0].account_seq == 1
    assert route.call_count == 2          # 429 → 백오프 후 재시도


@respx.mock
async def test_persistent_429_raises_after_retries():
    cfg = TossConfig(client_id="id", client_secret="sec", base_url=BASE,
                     account_seq=1, max_retries=1)
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(429, json={"error": {"code": "rate-limit-exceeded",
                                                         "message": "요청 한도를 초과했습니다."}})
    )
    async with TossClient(cfg) as c:
        with pytest.raises(TossAPIError) as ei:
            await c.get_accounts()
    assert ei.value.status == 429 and ei.value.code == "rate-limit-exceeded"


@respx.mock
async def test_resolves_account_seq_when_unset():
    cfg = TossConfig(client_id="id", client_secret="sec", base_url=BASE)  # account_seq None
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    respx.get(f"{BASE}/api/v1/accounts").mock(
        return_value=httpx.Response(200, json=fixture("accounts.json"))
    )
    route = respx.get(f"{BASE}/api/v1/holdings").mock(
        return_value=httpx.Response(200, json=fixture("holdings.json"))
    )
    async with TossClient(cfg) as c:
        await c.get_holdings()
    assert route.calls.last.request.headers.get("X-Tossinvest-Account") == "1"


# ── 캔들 ──────────────────────────────────────────────────────────────────────
@respx.mock
async def test_candles_params_and_ascending_sort(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    route = respx.get(f"{BASE}/api/v1/candles").mock(
        return_value=httpx.Response(200, json=fixture("candles.json"))
    )
    async with TossClient(cfg) as c:
        candles = await c.get_candles("005930", "1d")
    p = route.calls.last.request.url.params
    assert p["symbol"] == "005930" and p["interval"] == "1d"
    assert candles[0].timestamp < candles[-1].timestamp          # 과거→최신 정규화
    assert candles[-1].close_price == Decimal("310500")          # 06-23 최신
    assert candles[-1].volume == Decimal("76892183")


@respx.mock
async def test_candles_400_nested_data_field(cfg):
    respx.post(f"{BASE}/oauth2/token").mock(return_value=token_resp())
    respx.get(f"{BASE}/api/v1/candles").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": "invalid-request", "message": "요청 필드가 올바르지 않습니다.",
                            "data": {"field": "interval"}}},
        )
    )
    async with TossClient(cfg) as c:
        with pytest.raises(TossAPIError) as ei:
            await c.get_candles("005930", "1d")
    assert ei.value.code == "invalid-request" and ei.value.field == "interval"


async def test_candles_requires_interval_clientside(cfg):
    async with TossClient(cfg) as c:
        with pytest.raises(ValueError):
            await c.get_candles("005930", "")
