#!/usr/bin/env python3
"""
토스 Open API 진단 스모크 — 신규 프로젝트의 '첫 코드'.

목적   : 각 단계의 **원시 응답을 그대로 덤프**해 필드를 눈으로 확정한다.
         (인사이트 문서 §4 "빠른 검증 방법론" / §3 "추측 코드 금지"를 코드로 강제)
안전   : 이 스크립트는 **주문을 절대 전송하지 않는다**. 읽기 전용 호출만 한다.
         (POST /api/v1/orders 계열은 코드에 존재하지도 않는다.)
의존성 : 없음 — Python 표준 라이브러리만 사용한다(pip install 불필요).
         운영 코드는 httpx를 쓰지만, 스모크는 자격증명만 있으면 바로 돌도록 무의존성으로 짠다.

확인하는 함정 (인사이트 §2.2):
  1) 리소스 경로 prefix 는 /api/v1 (토큰만 /oauth2/token, prefix 없음)
  2) 모든 응답이 { "result": ... } 로 감싸여 온다 → unwrap
  3) X-Tossinvest-Account 헤더 = accountSeq(정수). 계좌번호 아님 → 자동 판별로 실증
  4) 금액은 통화버킷 중첩(루트) vs 평문(item) + item.currency, rate 는 분수
  5/7) /prices·/stocks 는 symbols 필수, buying-power 는 currency 필수

실행:
  1) 이 스크립트 옆(또는 server/) 에 .env 작성 — .env.example 참고:
       TOSS_CLIENT_ID=...
       TOSS_CLIENT_SECRET=...
  2) python server/scripts/toss_smoke.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 한국어 Windows 콘솔(cp949)에서도 유니코드/박스문자가 깨지지 않게.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"   # ⚠️ 이 엔드포인트만 /api/v1 prefix 없음
API = "/api/v1"                # ⚠️ 리소스는 전부 /api/v1 (블로그의 /v1 아님 → edge-blocked)
TIMEOUT = 20

# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────

def load_dotenv() -> str | None:
    """스크립트 옆 → server/ → 현재 작업폴더 순으로 .env 를 찾아 환경변수에 주입."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, ".env"),
        os.path.join(here, "..", ".env"),   # server/.env
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            return os.path.normpath(path)
    return None


def http(method: str, path: str, headers: dict | None = None,
         body: str | None = None, params: dict | None = None):
    """(status, raw_text, parsed_json|None) 반환. 2xx/4xx 모두 본문을 캡처한다.

    토스는 4xx 에서도 유용한 에러 JSON({code,message,field})을 주므로 반드시 본문을 읽는다.
    """
    url = path if path.startswith("http") else BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 네트워크/TLS 등
        return None, f"<request error: {e}>", None
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return status, text, parsed


def unwrap(parsed):
    """함정 2: 최상위 { "result": ... } 를 벗긴다."""
    if isinstance(parsed, dict) and "result" in parsed:
        return parsed["result"]
    return parsed


def auth_headers(token: str, account=None) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if account is not None:
        h["X-Tossinvest-Account"] = str(account)  # 정수 seq 라도 헤더는 문자열
    return h


def dump(title: str, status, parsed, raw_text: str = "") -> None:
    print("\n" + "═" * 74)
    print(f"▶ {title}    [HTTP {status}]")
    print("─" * 74)
    if parsed is not None:
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        print(raw_text or "<no body>")


def mask_token(parsed):
    """토큰 응답에서 access_token 만 가려서 보여준다(존재/형태만 확인)."""
    if not isinstance(parsed, dict):
        return parsed
    view = dict(parsed)
    tok = view.get("access_token")
    if isinstance(tok, str):
        view["access_token"] = f"<masked len={len(tok)} prefix={tok[:6]}…>"
    return view


def die(msg: str) -> None:
    print(f"\n[중단] {msg}")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────
# 메인 플로우
# ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    env_path = load_dotenv()
    print("토스 Open API 진단 스모크  (읽기 전용 · ❗주문 미전송)")
    print(f".env: {env_path or '없음 (OS 환경변수 사용)'}")
    print(f"BASE: {BASE}")

    cid = os.environ.get("TOSS_CLIENT_ID", "").strip()
    csec = os.environ.get("TOSS_CLIENT_SECRET", "").strip()
    placeholders = {"", "your_client_id_here", "your_client_secret_here"}
    if cid in placeholders or csec in placeholders:
        print("\n[중단] TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 아직 채워지지 않았습니다.")
        print("  토스증권 PC 웹 → 설정 → Open API 에서 발급 후 .env 에 넣으세요.")
        print("  파일: server/scripts/.env (템플릿: .env.example)")
        sys.exit(1)

    # 1) 토큰 — OAuth2 client_credentials, Basic auth, prefix 없음
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    status, text, parsed = http(
        "POST", TOKEN_PATH,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body="grant_type=client_credentials",
    )
    dump("POST /oauth2/token  (prefix 없음 · Basic auth · client_credentials)",
         status, mask_token(parsed), text)
    if status != 200 or not isinstance(parsed, dict) or "access_token" not in parsed:
        die("토큰 발급 실패. client_id / client_secret 를 확인하세요.")
    token = parsed["access_token"]
    print(f"\n  → token_type={parsed.get('token_type')!r}  "
          f"expires_in={parsed.get('expires_in')!r} (초)")

    # 2) 계좌 목록 — 계좌 헤더 불필요. 여기서 accountSeq / accountNo 획득
    status, text, parsed = http("GET", f"{API}/accounts", headers=auth_headers(token))
    dump("GET /api/v1/accounts  (계좌 헤더 불필요)", status, parsed, text)
    accounts = unwrap(parsed)
    if not isinstance(accounts, list) or not accounts:
        die("계좌 목록을 읽지 못했습니다.")
    acct = accounts[0]
    account_seq = acct.get("accountSeq")
    account_no = acct.get("accountNo")
    print(f"\n  → accountSeq={account_seq!r}  accountNo={account_no!r}  "
          f"type={acct.get('accountType')!r}")

    # 3) 계좌 식별자 자동 판별 (함정 3 실증):
    #    holdings 를 accountSeq / accountNo 두 값으로 시도 → 200 나오는 값이 정답
    print("\n" + "#" * 74)
    print("# 계좌 식별자 자동 판별: X-Tossinvest-Account 에 두 값을 넣어 holdings 시도")
    print("#   → 200 = 정답(예상: accountSeq) / 400 account-not-found = 오답")
    print("#" * 74)
    correct = None
    holdings = None
    for label, value in (("accountSeq", account_seq), ("accountNo", account_no)):
        if value is None:
            continue
        st, _tx, pj = http("GET", f"{API}/holdings", headers=auth_headers(token, value))
        mark = "✅" if st == 200 else "❌"
        detail = "" if st == 200 else f"  {(unwrap(pj) or pj)}"
        print(f"  {mark} X-Tossinvest-Account={value!r} ({label}) → HTTP {st}{detail}")
        if st == 200 and correct is None:
            correct, holdings = value, pj
    if correct is None:
        die("두 식별자 모두 holdings 실패. 계좌 상태/권한을 확인하세요.")
    print(f"\n  → 확정: X-Tossinvest-Account = {correct!r}")

    # 함정 4: 루트=통화버킷 중첩 {krw,usd}, items[]=평문+item.currency, rate=분수(×100)
    dump("GET /api/v1/holdings  (루트=통화버킷 중첩 · items[]=평문+currency · rate는 분수)",
         200, holdings, "")

    acct_hdr = auth_headers(token, correct)

    # 4) 매수가능금액 — currency 필수 (없으면 400 field: currency)
    st, tx, pj = http("GET", f"{API}/buying-power", headers=acct_hdr, params={"currency": "KRW"})
    dump("GET /api/v1/buying-power?currency=KRW  (currency 필수)", st, pj, tx)

    sym = os.environ.get("SMOKE_SYMBOL", "005930")

    # 5) 현재가 — symbols 필수. lastPrice(문자열). 등락률/거래량 없음
    st, tx, pj = http("GET", f"{API}/prices", headers=auth_headers(token), params={"symbols": sym})
    dump(f"GET /api/v1/prices?symbols={sym}  (symbols 필수 · 등락률/거래량 없음)", st, pj, tx)

    # 6) 종목 마스터 — symbols 필수. 섹터 없음, 위험판정용 플래그 풍부
    st, tx, pj = http("GET", f"{API}/stocks", headers=auth_headers(token), params={"symbols": sym})
    dump(f"GET /api/v1/stocks?symbols={sym}  (symbols 필수 · 섹터 없음 · 위험 플래그)", st, pj, tx)

    # 7) 종목별 경고 — 전역 목록 없음(종목 단위)
    st, tx, pj = http("GET", f"{API}/stocks/{sym}/warnings", headers=auth_headers(token))
    dump(f"GET /api/v1/stocks/{sym}/warnings  (종목별 경고 · 전역 목록 없음)", st, pj, tx)

    # 요약
    print("\n" + "═" * 74)
    print("스모크 완료.  ❗ 주문 엔드포인트(POST /api/v1/orders 계열)는 호출하지 않았다.")
    print("─" * 74)
    print(f"  확정 계좌 헤더 : X-Tossinvest-Account = {correct!r}")
    print("  금액 모델      : holdings 루트는 {krw,usd} 버킷, item은 평문+item.currency, "
          "profitLoss.rate는 분수(표시 시 ×100)")
    print("  다음 단계      : 위 원시 응답으로 Pydantic 모델 확정 → DRY_RUN 주문층/가드레일")
    print("═" * 74)


if __name__ == "__main__":
    main()
