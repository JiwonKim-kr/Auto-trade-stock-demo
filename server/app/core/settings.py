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
    # LLM 비용 가드: 틱당 매수 후보 상한(score 상위 N — 보유는 항상 평가) + 일일 판단 수 상한
    # (상한 도달 시 그날은 결정적 폴백으로 강등 — DB 필요, 근사 카운트)
    judge_top_n: int = 10
    daily_llm_decision_cap: int = 400

    # 내장 틱 루프(초). 0=비활성(운영은 Cloud Scheduler 가 /internal/tick 호출).
    # 로컬 상시 운용: 예 300 → 장중(KST 평일 09:00–15:30)에만 자동 틱.
    tick_interval_sec: int = 0

    # 캔들 TTL 캐시(분) — 일봉 재조회 낭비 방지(429 방어, DB 필요). 0=비활성
    candle_cache_ttl_minutes: int = 60

    # 알림(텔레그램) — 서킷브레이커 발동/해제·리컨실 불일치·자동 틱 실패·킬스위치 변경. 미설정=무음
    notify_telegram_bot_token: str | None = None
    notify_telegram_chat_id: str | None = None

    # 휴장일 자동 보고서 저장 폴더 + KRX 휴장일 파일(미설정 시 data/krx_holidays.json)
    reports_dir: str = "reports"
    krx_holidays_path: str | None = None

    # DB 영속화 (미설정 시 인메모리 — 재시작 시 원장/엔진 상태 소실. 운영은 필수)
    # 예: postgresql+asyncpg://user:pw@host/db (Cloud SQL) · sqlite+aiosqlite:///./trading.db (로컬)
    database_url: str | None = None

    # 페이퍼 P&L: DRY_RUN 의도 주문을 모의 체결해 전략 손익 추적(DB 필요). 0 이면 비활성
    # (비활성 시 틱은 실계좌 보유/현금으로 관찰만 — 기존 동작).
    paper_seed_krw: Decimal = Decimal("10000000")

    # 결정적 청산(LLM 우회 하드 룰): 손절·타임스톱 — 페이퍼 포지션 대상(LIVE 는 체결 추적 후)
    exit_rules_enabled: bool = True
    exit_stop_loss_rate: Decimal = Decimal("0.08")      # 취득단가 대비 -8% → 강제 청산
    exit_time_stop_days: int = 20                       # 보유 20 거래일 초과 → 강제 청산

    # 워치리스트: 쉼표 구분 종목코드. 명시 의도 → 심볼 소스보다 우선·항상 평가. 보유 종목도 항상 평가됨.
    watchlist: str = ""

    # 심볼 소스(외부 KRX 시드). 경로 지정 시 FileSymbolSource 로 전 종목 유니버스를 공급.
    # 미지정(기본)이면 워치리스트만 — 기존 동작 보존. 페처: scripts/fetch_krx_symbols.py
    symbol_source_path: str | None = None
    # 한 틱 후보 상한(캔들은 종목별 호출 → 레이트리밋 보호). 워치리스트 우선 포함분도 이 상한에 포함.
    universe_max_symbols: int = 40
    # 유니버스 2단계 선정: ADV 상위 풀 크기 + 탐색(미측정) 슬롯 비율(통계 없으면 순수 로테이션과 동등)
    adv_pool_size: int = 300
    universe_explore_ratio: float = 0.2

    # 가드레일 한도 (KRW)
    per_order_max_krw: Decimal = Decimal("100000")
    daily_buy_cap_krw: Decimal = Decimal("500000")
    max_positions: int = 10
    per_symbol_max_weight: Decimal = Decimal("0.10")   # 단일 종목 집중 차단(max_positions=10과 정합)
    enforce_market_hours: bool = True

    # 서킷브레이커: 손실 국면 신규 진입 자동 차단(청산은 허용). 낙폭은 rearm 까지 회복돼야 해제.
    daily_loss_limit: Decimal = Decimal("0.05")         # 일일 손실률 한도(양수 크기)
    max_drawdown_limit: Decimal = Decimal("0.15")       # 고점대비 낙폭 한도(양수 크기)
    drawdown_rearm: Decimal = Decimal("0.08")           # 낙폭 해제 기준(히스테리시스)

    # 비용 인지 진입 게이트: 기대이동폭 ≥ 라운드트립 비용 × 배수일 때만 매수(비용에 갉아먹히는 잔매매 차단)
    cost_commission_rate: Decimal = Decimal("0.00015")  # 편도 수수료
    cost_slippage_rate: Decimal = Decimal("0.0015")     # 편도 슬리피지(유동성별 보정 대상)
    cost_sell_tax_rate: Decimal = Decimal("0.0015")     # 증권거래세(매도). 2025~ 0.15%. ⚠️실거래 시 재확인
    entry_cost_multiple: Decimal = Decimal("3.5")       # 진입 문턱 = 라운드트립 × 이 값(0이면 게이트 사실상 해제)
    entry_move_multiple: Decimal = Decimal("3.0")       # 기대이동폭 = confidence × σ × 이 값

    # 레짐 필터: 시장 프록시 σ 국면별 신규 매수 노출 배수(거시=예측 아님·대응). 빈 값이면 비활성.
    regime_symbol: str = "069500"                       # KODEX 200 (시장 프록시)
    regime_calm_vol: Decimal = Decimal("0.010")         # 일간 σ < 1% → CALM(×1.0)
    regime_stress_vol: Decimal = Decimal("0.020")       # 일간 σ ≥ 2% → STRESS(신규 중단)
    regime_elevated_multiplier: Decimal = Decimal("0.5")
    regime_stress_multiplier: Decimal = Decimal("0")


@lru_cache
def get_settings() -> Settings:
    return Settings()
