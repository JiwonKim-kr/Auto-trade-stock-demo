"""KRX 상장 종목 심볼 시드 생성 — 토스로 열거 불가한 "전 종목" 출처(인사이트 §5 / 함정 5).

KRX 전자공시(kind.krx.co.kr) 상장법인목록 다운로드를 시장별로 받아 `(code, name, market)` 으로
파싱하고 `server/data/krx_symbols.json` 시드로 쓴다. **out-of-band**(틱 중 호출 아님) — 운영은
이 시드를 읽기만 해, 장중 스크래핑·네트워크 의존을 피한다(레이트리밋·장애 격리). 주기적으로 재실행해 갱신.

응답 실측(2026-06): HTTP 200, **EUC-KR HTML 테이블**(.xls 위장). 종목코드 셀은
`style="mso-number-format:'@';..."` 마커가 붙어 안정적 앵커가 된다. 회사명은 행의 첫 번째 td.
종목코드는 6자(우선주 등은 `0126Z0` 같은 영숫자도 있음 → 순수 숫자로 가정하지 않음).

실행: python server/scripts/fetch_krx_symbols.py   (KOSPI+KOSDAQ → data/krx_symbols.json)
의존성: 없음(stdlib). 읽기 전용 공개 데이터, 주문/자격증명과 무관.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "https://kind.krx.co.kr/corpgeneral/corpList.do"
MARKETS = {"stockMkt": "KOSPI", "kosdaqMkt": "KOSDAQ"}   # KONEX 제외(초저유동성)
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_CODE_TD_RE = re.compile(r"mso-number-format:'@';[^>]*>(.*?)</td>", re.S)


def fetch(market_type: str) -> str:
    query = urllib.parse.urlencode(
        {"method": "download", "searchType": "13", "marketType": market_type}
    )
    req = urllib.request.Request(f"{URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:        # noqa: S310 (고정 https URL)
        return resp.read().decode("euc-kr", errors="replace")


def parse(text: str, market: str) -> list[dict]:
    out: list[dict] = []
    for row in re.split(r"<tr>", text, flags=re.S)[2:]:          # [0]=프리앰블, [1]=헤더 → skip
        tds = _TD_RE.findall(row)
        if len(tds) < 3:
            continue
        m = _CODE_TD_RE.search(row)
        if not m:
            continue
        code = m.group(1).strip().upper()
        if code.isdigit():
            code = code.zfill(6)
        if not CODE_RE.match(code):
            continue
        name = html.unescape(re.sub(r"<.*?>", "", tds[0])).strip()
        out.append({"code": code, "name": name, "market": market})
    return out


def main() -> int:
    all_rows: list[dict] = []
    seen: set[str] = set()
    for market_type, market in MARKETS.items():
        text = fetch(market_type)
        rows = parse(text, market)
        kept = 0
        for r in rows:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            all_rows.append(r)
            kept += 1
        print(f"{market:7s} 파싱={len(rows):5d}  신규={kept}")

    all_rows.sort(key=lambda r: r["code"])
    out_path = Path(__file__).resolve().parents[1] / "data" / "krx_symbols.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "KRX kind.krx.co.kr corpList (searchType=13)",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(all_rows),
        "symbols": all_rows,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n총 {len(all_rows)}개 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
