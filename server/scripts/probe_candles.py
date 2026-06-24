#!/usr/bin/env python3
"""/candles 응답 형태 발견용 최소 프로브 (stdlib · 토큰 + candles 1콜만 — 레이트리밋 회피).

사용: python server/scripts/probe_candles.py "symbol=005930&interval=1d"
인자 없으면 기본 "symbol=005930". 400 이 오면 메시지의 field 로 필수 파라미터를 학습한다.
❗읽기 전용. 주문 미호출.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "https://openapi.tossinvest.com"


def load_env() -> None:
    here = Path(__file__).resolve().parent
    for p in (here / ".env", here.parent / ".env"):
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    load_env()
    cid, sec = os.environ.get("TOSS_CLIENT_ID"), os.environ.get("TOSS_CLIENT_SECRET")
    if not cid or not sec or cid == "your_client_id_here":
        print("[중단] 자격증명 없음")
        return 1
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    st, txt = http(
        "POST", f"{BASE}/oauth2/token",
        {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        b"grant_type=client_credentials",
    )
    if st != 200:
        print(f"[중단] 토큰 실패 {st}: {txt[:300]}")
        return 1
    tok = json.loads(txt)["access_token"]

    query = sys.argv[1] if len(sys.argv) > 1 else "symbol=005930"
    url = f"{BASE}/api/v1/candles?{query}"
    st, txt = http("GET", url, {"Authorization": f"Bearer {tok}"})
    print(f"GET /api/v1/candles?{query}   [HTTP {st}]")
    try:
        print(json.dumps(json.loads(txt), indent=2, ensure_ascii=False)[:3500])
    except Exception:
        print(txt[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
