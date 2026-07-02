"""레짐 필터 테스트 — 국면 판정(밴드·경계·표본부족) + LLM 컨텍스트 주입."""

from __future__ import annotations

from decimal import Decimal

from app.engine.costs import realized_daily_vol
from app.engine.llm import SYSTEM_PROMPT, CandidateContext, build_user_content
from app.engine.regime import RegimeConfig, RegimeLevel, assess_regime
from app.engine.screener import ScreenIndicators


def make_ctx(**kw) -> CandidateContext:
    base = dict(symbol="005930", name="삼성전자", market="KOSPI", currency="KRW",
                indicators=ScreenIndicators(last_close=70000.0, sma_short=68000.0,
                                            sma_long=65000.0, rsi=55.0, avg_volume=1_000_000.0),
                score=0.05, already_held=False)
    base.update(kw)
    return CandidateContext(**base)


def _alternating(swing: float, n: int = 13, base: float = 1000.0) -> list[float]:
    """±swing 교대 수익률 경로 → σ ≈ swing."""
    closes = [base]
    for i in range(n - 1):
        closes.append(closes[-1] * (1 + swing if i % 2 == 0 else 1 - swing))
    return closes


# ── 국면 판정 ─────────────────────────────────────────────────────────────────
def test_calm_regime_full_multiplier():
    r = assess_regime(_alternating(0.001))            # σ ≈ 0.1% < 1%
    assert r.level is RegimeLevel.CALM and r.multiplier == Decimal(1)


def test_elevated_regime_halves():
    r = assess_regime(_alternating(0.015))            # 1% ≤ σ ≈ 1.5% < 2%
    assert r.level is RegimeLevel.ELEVATED and r.multiplier == Decimal("0.5")


def test_stress_regime_blocks():
    r = assess_regime(_alternating(0.03))             # σ ≈ 3% ≥ 2%
    assert r.level is RegimeLevel.STRESS and r.multiplier == Decimal(0)
    assert "신규 진입 중단" in r.reason


def test_threshold_boundaries_inclusive():
    closes = _alternating(0.015)
    vol = realized_daily_vol(closes, min_returns=5)
    # σ == calm_vol → ELEVATED(≥), σ == stress_vol → STRESS(≥)
    assert assess_regime(closes, RegimeConfig(calm_vol=vol)).level is RegimeLevel.ELEVATED
    assert assess_regime(closes, RegimeConfig(stress_vol=vol)).level is RegimeLevel.STRESS


def test_unknown_on_insufficient_data():
    for closes in (None, [], [1000.0, 1001.0]):
        r = assess_regime(closes)
        assert r.level is RegimeLevel.UNKNOWN and r.multiplier == Decimal(1)


def test_lookback_uses_recent_window_only():
    # 과거는 폭풍(±5%), 최근 lookback 은 평온(±0.1%) → 최근 창만 보면 CALM
    closes = _alternating(0.05, n=30) + _alternating(0.001, n=25, base=1000.0)
    r = assess_regime(closes, RegimeConfig(lookback=20))
    assert r.level is RegimeLevel.CALM


def test_as_dict_serializable():
    d = assess_regime(_alternating(0.03)).as_dict()
    assert d["level"] == "STRESS" and d["multiplier"] == "0"
    assert d["daily_vol"] is not None


# ── LLM 층 통합 ───────────────────────────────────────────────────────────────
def test_system_prompt_forbids_macro_as_signal():
    assert "거시·지정학" in SYSTEM_PROMPT and "예측 시그널이 아니다" in SYSTEM_PROMPT


def test_user_content_includes_regime_when_set():
    c = make_ctx(market_regime="ELEVATED — 시장 σ 1.40% ≥ 1.0% — 노출 축소")
    assert "[시장 레짐] ELEVATED" in build_user_content(c)


def test_user_content_omits_regime_when_unset():
    assert "[시장 레짐]" not in build_user_content(make_ctx())
