"""심볼 소스 테스트 — 정규화·파일/정적 provider·해석(우선포함+상한). 네트워크 불필요."""

from __future__ import annotations

import json

import pytest

from app.engine.symbols import (
    FileSymbolSource,
    StaticSymbolSource,
    SymbolEntry,
    normalize_symbol,
    resolve_symbols,
)


# ── 정규화 ────────────────────────────────────────────────────────────────────
def test_normalize_zero_pads_numeric():
    assert normalize_symbol("5930") == "005930"
    assert normalize_symbol(" 660 ") == "000660"


def test_normalize_keeps_alphanumeric_preferred_codes():
    # 우선주/특수증권은 영숫자 코드(예: 0126Z0) — 순수 6자리 숫자로 가정하지 않는다
    assert normalize_symbol("0126z0") == "0126Z0"
    assert normalize_symbol("0120G0") == "0120G0"


def test_normalize_rejects_bad():
    assert normalize_symbol("") is None
    assert normalize_symbol("12345") == "012345"      # 5자리 숫자는 zero-pad
    assert normalize_symbol("ABCDEFG") is None         # 7자 초과
    assert normalize_symbol("00-660") is None          # 허용문자 외


# ── StaticSymbolSource ───────────────────────────────────────────────────────
async def test_static_source_normalizes_and_dedupes():
    src = StaticSymbolSource(["5930", "005930", "660", "bad-!"])
    codes = [e.code for e in await src.symbols()]
    assert codes == ["005930", "000660"]               # 중복·무효 제거, 순서보존


# ── FileSymbolSource ─────────────────────────────────────────────────────────
def _seed(tmp_path, payload):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


async def test_file_source_reads_fetcher_payload(tmp_path):
    p = _seed(tmp_path, {"symbols": [
        {"code": "005930", "name": "삼성전자", "market": "KOSPI"},
        {"code": "035720", "name": "카카오", "market": "KOSDAQ"},
    ]})
    entries = await FileSymbolSource(p).symbols()
    assert [e.code for e in entries] == ["005930", "035720"]
    assert entries[0].name == "삼성전자" and entries[0].market == "KOSPI"


async def test_file_source_accepts_bare_lists(tmp_path):
    p = _seed(tmp_path, ["5930", {"code": "660"}])     # 코드 문자열 + dict 혼합
    assert [e.code for e in await FileSymbolSource(p).symbols()] == ["005930", "000660"]


async def test_file_source_market_filter(tmp_path):
    p = _seed(tmp_path, {"symbols": [
        {"code": "005930", "market": "KOSPI"},
        {"code": "035720", "market": "KOSDAQ"},
    ]})
    entries = await FileSymbolSource(p, markets={"KOSPI"}).symbols()
    assert [e.code for e in entries] == ["005930"]


async def test_file_source_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await FileSymbolSource(tmp_path / "nope.json").symbols()


# ── resolve_symbols ──────────────────────────────────────────────────────────
class _FakeSource:
    def __init__(self, codes):
        self._e = [SymbolEntry(code=c) for c in codes]

    async def symbols(self):
        return list(self._e)


async def test_resolve_include_priority_and_limit():
    src = _FakeSource(["000660", "035720", "068270"])
    # include(워치리스트) 우선·전부, 소스는 상한 안에서 채움 → 전체 = limit
    out = await resolve_symbols(src, limit=3, include=["005930"])
    assert out == ["005930", "000660", "035720"]


async def test_resolve_dedupes_across_include_and_source():
    src = _FakeSource(["005930", "000660"])
    out = await resolve_symbols(src, limit=None, include=["5930"])
    assert out == ["005930", "000660"]                 # include 의 5930 == 소스 005930


async def test_resolve_no_limit_returns_all():
    src = _FakeSource(["000660", "035720"])
    out = await resolve_symbols(src, include=["005930"])
    assert out == ["005930", "000660", "035720"]
