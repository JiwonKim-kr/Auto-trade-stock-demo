"""앱 설정 — 환경변수(운영은 Secret Manager → env)에서 로드.

거래 모드(TRADING_MODE/LIVE 다중확인)는 일부러 여기 두지 않고 `core.config.load_trading_mode`로
분리한다(실자금 안전장치를 평범한 설정값으로 우회하지 못하게).
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # 데스크톱 ↔ 서버 인증 (운영 전 반드시 변경)
    api_key: str = "dev-local-key"

    # 토스 자격증명 (없으면 토스 연동 비활성 → 관련 엔드포인트 503)
    toss_client_id: str | None = None
    toss_client_secret: str | None = None
    toss_account_seq: int | None = None

    # AI 엔진 (없으면 틱은 결정적 폴백 판단기 사용 — 주문 데모 가능)
    anthropic_api_key: str | None = None
    research_top_n: int = 5

    # 워치리스트: 쉼표 구분 종목코드. 명시 의도 → 심볼 소스보다 우선·항상 평가. 보유 종목도 항상 평가됨.
    watchlist: str = ""

    # 심볼 소스(외부 KRX 시드). 경로 지정 시 FileSymbolSource 로 전 종목 유니버스를 공급.
    # 미지정(기본)이면 워치리스트만 — 기존 동작 보존. 페처: scripts/fetch_krx_symbols.py
    symbol_source_path: str | None = None
    # 한 틱 후보 상한(캔들은 종목별 호출 → 레이트리밋 보호). 워치리스트 우선 포함분도 이 상한에 포함.
    universe_max_symbols: int = 40

    # 가드레일 한도 (KRW)
    per_order_max_krw: Decimal = Decimal("100000")
    daily_buy_cap_krw: Decimal = Decimal("500000")
    max_positions: int = 10
    per_symbol_max_weight: Decimal = Decimal("0.30")
    enforce_market_hours: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
