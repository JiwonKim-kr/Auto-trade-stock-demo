"""유니버스 보수적 제외 — 종목 마스터(Stock) 플래그로 결정적 판정 (인사이트 §5).

제외 기준(마스터 실플래그가 1차):
  - 우선주          : isCommonShare == False
  - 레버리지/인버스 : leverageFactor != null
  - 비활성          : status != "ACTIVE"
  - 정리매매        : koreanMarketDetail.liquidationTrading
  - 거래정지        : koreanMarketDetail.krxTradingSuspended
  - SPAC/ETN        : securityType (보조로 이름 정규식)
이름 정규식은 **플래그가 빠졌을 때(None) 보조**로만 — 권위 있는 플래그와 모순내지 않는다.

⚠️ 함정 5: 토스로 전 종목 열거 불가 → **심볼 소스는 외부(KRX 종목목록 등)**. 이 모듈은
"외부에서 받은 후보 심볼 → 토스 stocks 마스터 enrich → 보수적 제외"의 마지막 단계다.
저유동성/동전주/종목별 경고(warnings)는 비용상 스크리너가 좁힌 **후보 단계에서 per-symbol** 적용.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from app.toss.models import Stock


class UniverseExclusion(str, Enum):
    PREFERRED_SHARE = "PREFERRED_SHARE"                    # 우선주(플래그)
    PREFERRED_SHARE_BY_NAME = "PREFERRED_SHARE_BY_NAME"    # 우선주(이름 보조)
    LEVERAGED_OR_INVERSE = "LEVERAGED_OR_INVERSE"          # 레버리지/인버스
    INACTIVE = "INACTIVE"                                  # status != ACTIVE
    LIQUIDATION_TRADING = "LIQUIDATION_TRADING"            # 정리매매
    TRADING_SUSPENDED = "TRADING_SUSPENDED"                # 거래정지
    EXCLUDED_SECURITY_TYPE = "EXCLUDED_SECURITY_TYPE"      # ETN/SPAC 등(타입)
    SPAC_BY_NAME = "SPAC_BY_NAME"                          # SPAC(이름 보조)
    ETN_BY_NAME = "ETN_BY_NAME"                            # ETN(이름 보조)


DEFAULT_EXCLUDED_SECURITY_TYPES = frozenset({"ETN", "SPAC"})

# 한국 우선주: 이름이 '우' / '우B' / '우1' 등으로 끝남 (예: 삼성전자우, 현대차2우B)
_PREFERRED_NAME = re.compile(r"우[0-9]*[A-Z]?$")
_SPAC_NAME = re.compile(r"스팩|기업인수목적")
_ETN_NAME = re.compile(r"ETN")


@dataclass(frozen=True)
class UniverseConfig:
    excluded_security_types: frozenset[str] = DEFAULT_EXCLUDED_SECURITY_TYPES
    enable_name_fallback: bool = True


@dataclass
class Exclusion:
    code: UniverseExclusion
    detail: str


@dataclass
class UniverseDecision:
    symbol: str
    eligible: bool
    exclusions: list[Exclusion] = field(default_factory=list)


def evaluate_stock(stock: Stock, cfg: UniverseConfig | None = None) -> UniverseDecision:
    cfg = cfg or UniverseConfig()
    ex: list[Exclusion] = []

    # ── 1차: 마스터 플래그(권위) ──
    if stock.is_common_share is False:
        ex.append(Exclusion(UniverseExclusion.PREFERRED_SHARE, "isCommonShare=false"))
    if stock.leverage_factor is not None:
        ex.append(Exclusion(UniverseExclusion.LEVERAGED_OR_INVERSE,
                            f"leverageFactor={stock.leverage_factor}"))
    if stock.status is not None and stock.status != "ACTIVE":
        ex.append(Exclusion(UniverseExclusion.INACTIVE, f"status={stock.status}"))
    md = stock.korean_market_detail
    if md is not None and md.liquidation_trading:
        ex.append(Exclusion(UniverseExclusion.LIQUIDATION_TRADING, "정리매매"))
    if md is not None and md.krx_trading_suspended:
        ex.append(Exclusion(UniverseExclusion.TRADING_SUSPENDED, "거래정지"))
    sec_type = (stock.security_type or "").upper()
    if sec_type and sec_type in cfg.excluded_security_types:
        ex.append(Exclusion(UniverseExclusion.EXCLUDED_SECURITY_TYPE,
                            f"securityType={stock.security_type}"))

    # ── 2차: 이름 정규식 (플래그가 없을 때만, 권위 플래그와 모순 회피) ──
    if cfg.enable_name_fallback:
        name = stock.name or ""
        if stock.is_common_share is None and _PREFERRED_NAME.search(name):
            ex.append(Exclusion(UniverseExclusion.PREFERRED_SHARE_BY_NAME, f"name~우: {name}"))
        if sec_type != "SPAC" and _SPAC_NAME.search(name):
            ex.append(Exclusion(UniverseExclusion.SPAC_BY_NAME, f"name~스팩: {name}"))
        if sec_type != "ETN" and _ETN_NAME.search(name):
            ex.append(Exclusion(UniverseExclusion.ETN_BY_NAME, f"name~ETN: {name}"))

    return UniverseDecision(symbol=stock.symbol, eligible=(len(ex) == 0), exclusions=ex)


def partition_universe(
    stocks: list[Stock], cfg: UniverseConfig | None = None
) -> tuple[list[Stock], list[UniverseDecision]]:
    """(적격 종목, 제외 결정[사유 포함]) 으로 분할. 제외는 감사/로깅용 사유를 보존."""
    cfg = cfg or UniverseConfig()
    eligible: list[Stock] = []
    excluded: list[UniverseDecision] = []
    for s in stocks:
        d = evaluate_stock(s, cfg)
        if d.eligible:
            eligible.append(s)
        else:
            excluded.append(d)
    return eligible, excluded


def filter_universe(stocks: list[Stock], cfg: UniverseConfig | None = None) -> list[Stock]:
    return partition_universe(stocks, cfg)[0]
