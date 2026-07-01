"""유니버스 심볼 소스 — 토스로 열거 불가한 "전 종목" 후보를 외부에서 공급 (인사이트 §5 / 함정 5).

토스 `/stocks` 는 `symbols` 지정 조회만 가능하고 **전체 상장 종목 목록 엔드포인트가 없다**. 따라서
거래 후보의 출처(심볼 소스)는 외부(KRX)에서 마련해 토스 마스터로 enrich 한다. 이 모듈은 그 출처를
추상화(`SymbolSource`)하고, 운영 기본인 **파일 시드**(out-of-band 페처가 갱신)와 정적 리스트를 제공한다.

⚠️ 레이트 리밋 주의: 캔들은 **종목별 호출**이라 전 종목(KOSPI+KOSDAQ ≈ 2,800)을 한 틱에 다 돌리면
429. 그래서 `resolve_symbols(limit=...)` 로 한 틱 후보 수를 상한한다. 유동성/시총 기반 지능형
사전선별(top-N)은 다음 단계 — 지금은 단순 상한이 스톱갭이다.

심볼 정규화는 `^[0-9A-Z]{6}$` — 한국 종목코드는 6자다. 우선주/특수증권은 숫자가 아닌 코드(예:
`0126Z0`)도 있어 **순수 6자리 숫자로 가정하지 않는다**. 우선주·레버리지 등 제외 판정은 이 모듈이
아니라 `universe.py`(토스 마스터 플래그·권위)가 한다 — 심볼 소스는 "열거"만 한다(단일 책임).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


def normalize_symbol(raw: str) -> str | None:
    """종목코드 정규화: 공백제거·대문자·숫자면 6자리 zero-pad. 형식 위반은 None(드롭)."""
    s = (raw or "").strip().upper()
    if not s:
        return None
    if s.isdigit():
        s = s.zfill(6)
    return s if _CODE_RE.match(s) else None


@dataclass(frozen=True)
class SymbolEntry:
    code: str
    name: str = ""
    market: str | None = None        # "KOSPI" / "KOSDAQ"


class SymbolSource(Protocol):
    async def symbols(self) -> list[SymbolEntry]: ...


class StaticSymbolSource:
    """명시 코드 리스트(예: 워치리스트). 정규화·중복제거(순서보존)."""

    def __init__(self, codes: Iterable[str]):
        seen: set[str] = set()
        entries: list[SymbolEntry] = []
        for raw in codes:
            code = normalize_symbol(raw)
            if code and code not in seen:
                seen.add(code)
                entries.append(SymbolEntry(code=code))
        self._entries = entries

    async def symbols(self) -> list[SymbolEntry]:
        return list(self._entries)


class FileSymbolSource:
    """JSON 시드 파일에서 심볼을 읽는다(운영 기본). 페처가 out-of-band 로 갱신.

    허용 형태:
      - {"symbols": [{"code","name","market"}, ...]}  (페처 출력)
      - [{"code","name","market"}, ...]
      - ["005930", "000660", ...]                      (코드 문자열 리스트)

    `markets` 지정 시 해당 시장만(예: {"KOSPI"}). 파일 부재는 명시적 오류(설정 실수 가시화).
    """

    def __init__(self, path: str | Path, markets: Iterable[str] | None = None):
        self.path = Path(path)
        self.markets = {m.upper() for m in markets} if markets else None

    async def symbols(self) -> list[SymbolEntry]:
        if not self.path.is_file():
            raise FileNotFoundError(f"심볼 시드 파일 없음: {self.path} (페처로 생성: fetch_krx_symbols.py)")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rows = data.get("symbols", []) if isinstance(data, dict) else data

        seen: set[str] = set()
        out: list[SymbolEntry] = []
        for row in rows:
            if isinstance(row, str):
                code, name, market = normalize_symbol(row), "", None
            else:
                code = normalize_symbol(row.get("code", ""))
                name = (row.get("name") or "").strip()
                market = (row.get("market") or None)
            if not code or code in seen:
                continue
            if self.markets is not None and (market or "").upper() not in self.markets:
                continue
            seen.add(code)
            out.append(SymbolEntry(code=code, name=name, market=market))
        return out


async def resolve_symbols(
    source: SymbolSource,
    *,
    limit: int | None = None,
    include: Iterable[str] = (),
) -> list[str]:
    """소스 + 우선포함(워치리스트) → 정규화·중복제거 코드 리스트(순서보존).

    `include`(워치리스트)는 **항상 우선·전부 유지**, 소스는 `limit` 안에서 채운다(전체 상한 = limit).
    캔들 호출 수를 묶어 레이트리밋을 보호한다(보유 종목은 run_tick 이 별도 union — 매도 평가).
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in include:                          # 워치리스트: 명시 의도 → 무조건 우선
        code = normalize_symbol(raw)
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    for entry in await source.symbols():
        if limit is not None and len(result) >= limit:
            break
        if entry.code not in seen:
            seen.add(entry.code)
            result.append(entry.code)
    return result
