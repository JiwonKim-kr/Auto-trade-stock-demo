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


# ── 코호트 로테이션 (offset) ──────────────────────────────────────────────────
async def test_resolve_offset_rotates_cohorts():
    src = _FakeSource(["A00001", "B00002", "C00003", "D00004"])
    assert await resolve_symbols(src, limit=2, offset=0) == ["A00001", "B00002"]
    assert await resolve_symbols(src, limit=2, offset=2) == ["C00003", "D00004"]
    assert await resolve_symbols(src, limit=2, offset=4) == ["A00001", "B00002"]   # 한 바퀴


async def test_resolve_offset_wraps_around():
    src = _FakeSource(["A00001", "B00002", "C00003"])
    assert await resolve_symbols(src, limit=2, offset=2) == ["C00003", "A00001"]   # 경계 넘어 순환


async def test_resolve_offset_keeps_include_priority():
    src = _FakeSource(["A00001", "B00002", "C00003"])
    out = await resolve_symbols(src, limit=3, include=["005930"], offset=1)
    assert out == ["005930", "B00002", "C00003"]               # 워치리스트 항상 선두


# ── 2단계 유니버스 선정 (ADV 활용 + 탐색) ─────────────────────────────────────
SEED = ["A00001", "B00002", "C00003", "D00004", "E00005"]


def test_universe_cold_start_equals_rotation():
    # 통계 없음 → 탐색+폴백이 시드 로테이션과 동등(첫 코호트 = 시드 앞부분)
    from app.engine.symbols import resolve_universe
    assert resolve_universe(SEED, limit=2, tick_count=0) == ["A00001", "B00002"]
    out1 = resolve_universe(SEED, limit=2, tick_count=1)
    assert out1 != ["A00001", "B00002"]                        # 틱마다 다른 코호트


def test_universe_exploit_prefers_adv_pool():
    from app.engine.symbols import resolve_universe
    out = resolve_universe(SEED, limit=4, tick_count=0,
                           adv_pool=["E00005", "D00004"], fresh=set(SEED))
    assert out[:2] == ["E00005", "D00004"]                     # 활용 슬롯 = ADV 상위 우선


def test_universe_explore_targets_stale_only():
    from app.engine.symbols import resolve_universe
    # fresh 4개 → stale = C00003 만. limit 4 → 탐색 슬롯(1)이 stale 을 잡는다
    out = resolve_universe(SEED, limit=4, tick_count=0,
                           adv_pool=["A00001", "B00002", "D00004"],
                           fresh={"A00001", "B00002", "D00004", "E00005"})
    assert "C00003" in out


def test_universe_include_first_and_no_dupes():
    from app.engine.symbols import resolve_universe
    out = resolve_universe(SEED, limit=3, include=["A00001"], tick_count=0,
                           adv_pool=["A00001", "B00002"], fresh=set())
    assert out[0] == "A00001" and len(out) == len(set(out)) == 3


def test_universe_coverage_over_cycle():
    # 콜드스타트에서 여러 틱을 돌리면 전 시드가 커버된다(편향 없음)
    from app.engine.symbols import resolve_universe
    covered = set()
    for t in range(6):
        covered.update(resolve_universe(SEED, limit=2, tick_count=t))
    assert covered == set(SEED)
