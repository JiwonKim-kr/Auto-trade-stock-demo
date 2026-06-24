"""유니버스 보수적 제외 테스트."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.engine.universe import (
    UniverseConfig,
    UniverseExclusion,
    evaluate_stock,
    filter_universe,
    partition_universe,
)
from app.toss.models import KoreanMarketDetail, Stock

FIX = Path(__file__).parent / "fixtures"


def mk(name: str = "삼성전자", symbol: str = "005930", **kw) -> Stock:
    return Stock(symbol=symbol, name=name, **kw)


def codes(stock: Stock, cfg: UniverseConfig | None = None) -> set[UniverseExclusion]:
    return {e.code for e in evaluate_stock(stock, cfg).exclusions}


def test_common_stock_eligible_from_fixture():
    raw = json.loads((FIX / "stocks.json").read_text(encoding="utf-8"))["result"][0]
    s = Stock.model_validate(raw)
    d = evaluate_stock(s)
    assert d.eligible and d.exclusions == []


def test_preferred_by_flag():
    s = mk(name="삼성전자우", symbol="005935", is_common_share=False)
    assert UniverseExclusion.PREFERRED_SHARE in codes(s)
    assert not evaluate_stock(s).eligible


def test_preferred_by_name_when_flag_missing():
    s = mk(name="삼성전자우", symbol="005935")           # is_common_share None
    assert UniverseExclusion.PREFERRED_SHARE_BY_NAME in codes(s)


def test_common_flag_overrides_name():
    s = mk(name="이상한우", is_common_share=True)         # 권위 플래그가 보통주
    assert UniverseExclusion.PREFERRED_SHARE_BY_NAME not in codes(s)
    assert evaluate_stock(s).eligible


def test_leveraged_excluded():
    s = mk(name="KODEX 레버리지", symbol="122630", leverage_factor=Decimal("2"))
    assert UniverseExclusion.LEVERAGED_OR_INVERSE in codes(s)


def test_inactive_excluded():
    assert UniverseExclusion.INACTIVE in codes(mk(status="DELISTED"))
    assert evaluate_stock(mk(status="ACTIVE")).eligible


def test_liquidation_and_suspension():
    s1 = mk(korean_market_detail=KoreanMarketDetail(liquidation_trading=True,
                                                    krx_trading_suspended=False))
    assert UniverseExclusion.LIQUIDATION_TRADING in codes(s1)
    s2 = mk(korean_market_detail=KoreanMarketDetail(liquidation_trading=False,
                                                    krx_trading_suspended=True))
    assert UniverseExclusion.TRADING_SUSPENDED in codes(s2)


def test_etn_by_type_no_duplicate_name():
    s = mk(name="삼성 레버리지 WTI원유 선물 ETN", symbol="530031", security_type="ETN")
    c = codes(s)
    assert UniverseExclusion.EXCLUDED_SECURITY_TYPE in c
    assert UniverseExclusion.ETN_BY_NAME not in c        # 타입으로 이미 걸림 → 이름 중복 안 함


def test_spac_by_name():
    s = mk(name="엔에이치스팩30호", symbol="123456")
    assert UniverseExclusion.SPAC_BY_NAME in codes(s)


def test_multiple_reasons_accumulate():
    s = mk(name="머시기우", is_common_share=False, leverage_factor=Decimal("1"), status="SUSPENDED")
    c = codes(s)
    assert {
        UniverseExclusion.PREFERRED_SHARE,
        UniverseExclusion.LEVERAGED_OR_INVERSE,
        UniverseExclusion.INACTIVE,
    } <= c


def test_partition_and_filter():
    good = mk(symbol="005930", name="삼성전자", is_common_share=True, status="ACTIVE")
    bad = mk(symbol="005935", name="삼성전자우", is_common_share=False)
    eligible, excluded = partition_universe([good, bad])
    assert [s.symbol for s in eligible] == ["005930"]
    assert [d.symbol for d in excluded] == ["005935"]
    assert filter_universe([good, bad]) == [good]
