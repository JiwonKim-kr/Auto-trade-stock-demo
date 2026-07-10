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


def _rotate(items: list[str], offset: int) -> list[str]:
    if not items:
        return items
    k = offset % len(items)
    return items[k:] + items[:k]


def resolve_universe(
    seed_codes: list[str],
    *,
    limit: int,
    include: Iterable[str] = (),
    tick_count: int = 0,
    adv_pool: list[str] | None = None,
    fresh: frozenset[str] | set[str] = frozenset(),
    explore_ratio: float = 0.2,
) -> list[str]:
    """유니버스 2단계 선정 — ADV 상위 풀 활용(exploit) + 미측정/낡음 탐색(explore) (PLAN §2.2).

    슬롯: exploit = limit×(1−ratio) 를 adv_pool 로테이션에서, explore = 나머지를 stale
    (fresh 에 없는 시드 심볼) 로테이션에서 채운다. 부족분은 시드 전체 로테이션 폴백 —
    **콜드스타트(통계 없음)에는 자연히 순수 로테이션과 동등**하게 동작하고, 통계가 쌓이면
    유동성 상위에 판단 예산이 집중된다. 워치리스트(include)는 항상 우선.
    """
    seen: set[str] = set()
    result: list[str] = []

    def take(stream: list[str], cap: int) -> None:
        for code in stream:
            if len(result) >= cap:
                return
            if code not in seen:
                seen.add(code)
                result.append(code)

    for raw in include:
        code = normalize_symbol(raw)
        if code and code not in seen:
            seen.add(code)
            result.append(code)

    n_explore = max(1, int(-(-limit * explore_ratio // 1)))          # ceil, 최소 1(탐색 보장)
    n_exploit = max(0, limit - n_explore)
    base = len(result)
    take(_rotate(list(adv_pool or []), tick_count * max(n_exploit, 1)), base + n_exploit)
    stale = [c for c in seed_codes if c not in fresh]
    take(_rotate(stale, tick_count * n_explore), limit)
    take(_rotate(seed_codes, tick_count * limit), limit)             # 폴백(콜드스타트 커버)
    return result


async def resolve_symbols(
    source: SymbolSource,
    *,
    limit: int | None = None,
    include: Iterable[str] = (),
    offset: int = 0,
) -> list[str]:
    """소스 + 우선포함(워치리스트) → 정규화·중복제거 코드 리스트(순서보존).

    `include`(워치리스트)는 **항상 우선·전부 유지**, 소스는 `limit` 안에서 채운다(전체 상한 = limit).
    캔들 호출 수를 묶어 레이트리밋을 보호한다(보유 종목은 run_tick 이 별도 union — 매도 평가).

    `offset`: 소스를 wrap-around 회전시켜 읽기 시작할 위치 — **코호트 로테이션**.
    limit 로 자르면 시드 앞쪽(코드 오름차순)만 반복 평가되는 편향이 생긴다 → 호출자가
    `틱 수 × limit` 을 넘기면 전 유니버스가 틱마다 다음 코호트로 공평하게 순환한다.
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in include:                          # 워치리스트: 명시 의도 → 무조건 우선
        code = normalize_symbol(raw)
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    entries = await source.symbols()
    if offset and entries:
        k = offset % len(entries)
        entries = entries[k:] + entries[:k]      # wrap-around 로테이션
    for entry in entries:
        if limit is not None and len(result) >= limit:
            break
        if entry.code not in seen:
            seen.add(entry.code)
            result.append(entry.code)
    return result
