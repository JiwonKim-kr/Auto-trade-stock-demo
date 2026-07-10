"""거래 틱 오케스트레이션 — 수집 → 유니버스 → 스크리너 → 조사 → 판단 → 사이징 → DRY_RUN 주문.

전 파이프라인을 잇는 백본. 모든 의존성(toss 클라이언트·판단기·조사기·주문서비스)을 주입받아
테스트 가능하다. DRY_RUN 에선 실주문이 나가지 않는다(주문층이 보장).

⚠️ 유니버스 심볼 소스(KRX)는 아직 외부 연동 전이라, watchlist(임시 유니버스) ∪ 보유 종목을 평가한다.
보유 종목은 유니버스/스크리너 통과 여부와 무관하게 매도 평가 대상으로 포함된다.
캔들은 종목별 호출이라 레이트리밋 주의(클라이언트가 429 백오프 재시도) — watchlist 는 작게.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.engine.allocator import allocate
from app.engine.costs import EntryGate
from app.engine.exits import ForcedExit
from app.engine.llm import (
    Action,
    CandidateContext,
    Decision,
    DecisionProvider,
    candidate_contexts,
    decide_candidates,
)
from app.engine.regime import RegimeAssessment, RegimeConfig, RegimeLevel, assess_regime
from app.engine.research import ResearchProvider, research_candidates
from app.engine.screener import ScreenConfig, ScreenResult, screen_symbol
from app.engine.universe import partition_universe
from app.orders.context import context_from_holdings
from app.orders.models import OrderResult, OrderStatus, Side
from app.orders.service import OrderService


@dataclass
class TickResult:
    mode: str
    kill_switch: bool
    universe_symbols: list[str]
    candidates: int
    decisions: list[Decision]
    orders: list[OrderResult]
    note: str = ""
    circuit_breaker: bool = False
    circuit_breaker_reason: str = ""
    cost_gated: list[str] = field(default_factory=list)   # 비용 게이트로 차단된 매수 후보
    regime: dict = field(default_factory=dict)             # 레짐 판정(level·σ·배수) — 미평가 시 빈 dict
    forced_exits: list[dict] = field(default_factory=list)  # 결정적 청산(손절·타임스톱) 발동 내역
    adv20: dict = field(default_factory=dict)               # 심볼→20일 평균 거래대금(float) — 유동성 축적


class DeterministicJudge:
    """LLM 미설정 시 폴백 — 스크리너 통과 매수후보는 BUY(score 비례 confidence), 보유는 HOLD."""

    async def decide(self, ctx: CandidateContext) -> Decision:
        if not ctx.already_held and ctx.indicators is not None:
            conf = min(max(ctx.score * 10, 0.0), 1.0)
            action = Action.BUY if conf > 0 else Action.HOLD
            return Decision(action=action, symbol=ctx.symbol, confidence=conf,
                            rationale="결정적 폴백: 스크리너 통과 매수후보")
        return Decision(action=Action.HOLD, symbol=ctx.symbol, confidence=0.3,
                        rationale="결정적 폴백: 보유 유지")


async def run_tick(
    *,
    toss,
    order_service: OrderService,
    watchlist: list[str],
    judge: DecisionProvider,
    now: datetime,
    research: ResearchProvider | None = None,
    screen_config: ScreenConfig | None = None,
    research_top_n: int | None = 5,
    entry_gate: EntryGate | None = None,
    daily_buy_used_krw: Decimal = Decimal(0),
    regime_config: RegimeConfig | None = None,
    holdings=None,
    cash_buying_power_krw: Decimal | None = None,
    max_buy_candidates: int | None = None,
    forced_exits: list[ForcedExit] | None = None,
) -> TickResult:
    screen_config = screen_config or ScreenConfig()
    mode, ks = order_service.mode.value, order_service.kill_switch

    cb = order_service.circuit_breaker
    regime: RegimeAssessment | None = None
    forced = {f.symbol: f for f in (forced_exits or [])}

    adv20: dict[str, float] = {}

    def _result(candidates, decisions, orders, universe, note="", cost_gated=None) -> TickResult:
        return TickResult(mode=mode, kill_switch=ks, universe_symbols=universe,
                          candidates=candidates, decisions=decisions, orders=orders, note=note,
                          circuit_breaker=cb.tripped, circuit_breaker_reason=cb.reason,
                          cost_gated=cost_gated or [],
                          regime=regime.as_dict() if regime else {},
                          forced_exits=[f.as_dict() for f in forced.values()],
                          adv20=adv20)

    # 1) 수집: 보유 + 워치리스트 → 심볼 union. holdings 는 호출자가 선조회해 주입 가능
    #    (라우트가 리컨실에 이미 조회한 것을 재사용 — 레이트리밋 보호, 이중 조회 방지)
    if holdings is None:
        holdings = await toss.get_holdings()
    held_symbols = [i.symbol for i in holdings.items]
    symbols = list(dict.fromkeys([*watchlist, *held_symbols]))
    if not symbols:
        return _result(0, [], [], [], note="평가할 심볼 없음(워치리스트·보유 비어있음)")

    # 2) 종목 마스터
    stocks = {s.symbol: s for s in await toss.get_stocks(symbols)}

    # 3) 유니버스 보수적 제외 (보유는 매도 평가 위해 통과)
    eligible_stocks, _excluded = partition_universe([stocks[s] for s in symbols if s in stocks])
    eligible = {s.symbol for s in eligible_stocks} | set(held_symbols)

    # 4) 캔들 → 지표/스크리닝 (+ ADV20 공짜 축적 — 유니버스 2단계 선정 입력)
    buy_results: list[ScreenResult] = []
    holding_indicators: dict = {}
    recent: dict = {}
    for sym in symbols:
        if sym not in eligible:
            continue
        candles = await toss.get_candles(sym, "1d")
        result = screen_symbol(sym, candles, screen_config)
        recent[sym] = [float(c.close_price) for c in candles]
        tail = candles[-20:]
        if tail:
            adv20[sym] = sum(float(c.close_price) * float(c.volume) for c in tail) / len(tail)
        if sym in held_symbols:
            holding_indicators[sym] = result.indicators
        elif result.passed:
            buy_results.append(result)

    # 4b) 매수 후보 압축(LLM 비용 가드) — score 상위 N 만 판단에 올린다. 보유는 항상 평가(매도 안전).
    if max_buy_candidates is not None and len(buy_results) > max_buy_candidates:
        buy_results = sorted(buy_results, key=lambda r: r.score, reverse=True)[:max_buy_candidates]

    # 5) 매수여력 — 호출자가 주입 가능(페이퍼 모드: 페이퍼 현금으로 자기일관 사이징)
    if cash_buying_power_krw is not None:
        cash = cash_buying_power_krw
    else:
        try:
            cash = (await toss.get_buying_power("KRW")).cash_buying_power
        except Exception:
            cash = None

    # 5b) 서킷브레이커 갱신(틱당 1회). 자기자본=현금+보유 평가액(KRW), 일일손익률=holdings.
    #     발동 시 신규 매수만 차단(매도=청산은 허용). 상태 주입은 order_service.submit 이 처리.
    equity = (cash + holdings.market_value.amount.krw) if cash is not None else None
    daily_pl_rate = holdings.daily_profit_loss.rate if holdings.daily_profit_loss else None
    order_service.assess_circuit_breaker(equity, daily_pl_rate, now)

    # 6) 후보 컨텍스트 (매수 후보 + 보유 종목)
    candidates = candidate_contexts(buy_results, stocks, holdings,
                                    holding_indicators=holding_indicators,
                                    recent_closes=recent, cash_buying_power_krw=cash)
    if not candidates:
        return _result(0, [], [], sorted(eligible), note="후보 없음")

    # 6b) 레짐 필터(선택) — 시장 프록시 σ → 노출 배수. 조회 실패는 UNKNOWN(배수 1.0, 종목별
    #     방어는 게이트·가드레일이 담당). 판정 요약은 LLM 컨텍스트([시장 레짐])로도 전달.
    if regime_config is not None:
        try:
            regime_candles = await toss.get_candles(regime_config.symbol, "1d")
            regime = assess_regime([float(c.close_price) for c in regime_candles], regime_config)
        except Exception:
            regime = RegimeAssessment(RegimeLevel.UNKNOWN, None, Decimal(1),
                                      f"레짐 프록시({regime_config.symbol}) 조회 실패 — 배수 1.0")
        for c in candidates:
            c.market_regime = f"{regime.level.value} — {regime.reason}"

    # 7) 조사 (상위 N) — 선택. 강제 청산 심볼은 조사·판단 모두 제외(비용 절약 + LLM 우회)
    judge_candidates = [c for c in candidates if c.symbol not in forced]
    if research is not None:
        await research_candidates(judge_candidates, research, top_n=research_top_n)

    # 8) 판단 — 결정적 청산(손절·타임스톱)이 최우선(LLM 이 HOLD 로 뒤집을 수 없다)
    decisions = [
        Decision(action=Action.SELL, symbol=f.symbol, confidence=1.0,
                 rationale=f"결정적 청산: {f.reason}")
        for f in forced.values() if f.symbol in {c.symbol for c in candidates}
    ]
    decisions += await decide_candidates(judge_candidates, judge)
    ctx_lookup = {c.symbol: c for c in candidates}
    for d in decisions:                    # 판단 시점 종가 주입 — confidence 캘리브레이션 데이터
        c = ctx_lookup.get(d.symbol)
        if d.decision_price is None and c is not None and c.indicators is not None:
            d.decision_price = c.indicators.last_close

    # 9) 사이징 + 주문 (DRY_RUN; 실주문 0은 주문층이 보장)
    ctx_by = {c.symbol: c for c in candidates}
    base_ctx = context_from_holdings(holdings, now, kill_switch=ks)
    # 오늘 이미 쓴 매수액에서 시작(호출자가 DB 합산 주입) — 일일 한도를 틱 경계 너머로 강제
    daily_used = daily_buy_used_krw
    orders: list[OrderResult] = []
    cost_gated: list[str] = []
    for d in decisions:
        ctx = ctx_by[d.symbol]
        # 비용 인지 진입 게이트(선택): 기대이동폭이 라운드트립 비용 문턱을 못 넘는 매수는 차단(매도/보유 무관)
        if entry_gate is not None and d.action is Action.BUY:
            if not entry_gate.evaluate(d.confidence, ctx.recent_closes).passed:
                cost_gated.append(d.symbol)
                continue
        req = allocate(d, ctx, order_service.config,
                       exposure_multiplier=regime.multiplier if regime else Decimal(1))
        if req is None:
            continue
        price = Decimal(str(ctx.indicators.last_close)) if ctx.indicators else Decimal(0)
        sym_value = (ctx.held_quantity or Decimal(0)) * price
        gctx = dataclasses.replace(base_ctx, daily_buy_used_krw=daily_used,
                                   symbol_current_value_krw=sym_value)
        res = order_service.submit(req, gctx)
        orders.append(res)
        if req.side is Side.BUY and res.status in (OrderStatus.DRY_RUN, OrderStatus.SUBMITTED):
            daily_used += req.estimated_notional() or Decimal(0)   # 틱 내 일일한도 누적

    return _result(len(candidates), decisions, orders, sorted(eligible), cost_gated=cost_gated)
