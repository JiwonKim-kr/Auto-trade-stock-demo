"""틱 오케스트레이션 — 라우트(/internal/tick)와 내장 루프가 공유하는 실행 경로.

조립 순서(경계 계층 — DB/토스 I/O 는 여기, run_tick 은 순수 오케스트레이션):
  직렬화 락 → 유니버스 로테이션 → 판단기 선택(LLM 비용가드 강등 포함) → 게이트/레짐 구성
  → 리컨실(실계좌, LIVE 불일치 = 킬스위치) → 페이퍼 장부 로드/마킹(합성 Holdings)
  → run_tick → 모의 체결·기록(틱/결정/주문/엔진 상태/자산곡선).

내장 틱 루프(tick_interval_sec>0): 로컬 상시 운용용 — 장중(KST 평일 09:00–15:30)에만 자동 틱.
운영(Cloud Run)은 0 으로 두고 Cloud Scheduler 가 /internal/tick 을 호출한다(중복은 락이 직렬화).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI

from app.db.repo import trade_date_kst
from app.engine.costs import CostConfig, EntryGate, EntryGateConfig
from app.engine.exits import ExitConfig, evaluate_exits
from app.engine.llm import ClaudeJudge
from app.engine.paper import PaperPortfolio
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.regime import RegimeConfig
from app.engine.research import WebSearchResearch
from app.engine.symbols import FileSymbolSource, resolve_universe
from app.orders.guardrails import KST
from app.orders.models import OrderStatus, TradingMode
from app.orders.reconcile import reconcile, snapshot_from_holdings
from app.orders.service import OrderService
from app.toss.caching import CachingToss
from app.toss.models import Holdings

logger = logging.getLogger("app.tick")


async def reconcile_and_enforce(
    repo, svc: OrderService, holdings: Holdings, now: datetime, *, advance_baseline: bool,
    notifier=None, alert_gate=None,
) -> dict:
    """리컨실 실행 + 집행: 불일치 감사 기록·알림, LIVE 면 킬스위치 자동 발동(거래 중단).

    advance_baseline: 틱은 True(기준선 전진 — 감지된 외부 변화를 흡수해 반복 경보 방지),
    수동 점검(/api/reconcile)은 False(읽기 전용). 기준선 없으면 어느 쪽이든 생성.
    """
    items = snapshot_from_holdings(holdings)
    current = {i.symbol: i.quantity for i in items}
    prev = await repo.load_latest_positions()

    if prev is None:
        report = reconcile(None, current)
        await repo.save_positions_snapshot(now, items)          # 기준선 생성
        return report.as_dict()

    prev_ts, prev_map = prev
    delta = await repo.submitted_qty_since(prev_ts)
    report = reconcile(prev_map, current, delta)
    if not report.ok:
        await repo.audit("system", "reconcile_mismatch", report.as_dict())
        halted = False
        if svc.mode is TradingMode.LIVE and not svc.kill_switch:
            svc.engage_kill_switch()                             # 실자금 위 불일치 → 거래 중단
            halted = True
            await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())
            await repo.audit("system", "kill_switch",
                             {"engaged": True, "cause": "reconcile_mismatch"})
        if notifier is not None and alert_gate is not None:
            key = "reconcile:" + ",".join(
                f"{d.symbol}:{d.kind.value}" for d in report.discrepancies)
            if alert_gate.allow(key):                            # 같은 불일치는 60분 1회
                summary = "; ".join(f"{d.symbol} {d.kind.value}" for d in report.discrepancies)
                await notifier.send("⚠️ 리컨실 불일치: " + summary
                                    + (" → 킬스위치 자동 발동" if halted else ""))
    if advance_baseline:
        await repo.save_positions_snapshot(now, items)
    return report.as_dict()


async def execute_tick(app: FastAPI) -> dict:
    """틱 1회 실행(전 조립). 동시 호출은 락으로 직렬화 — 진행 중이면 스킵 응답."""
    lock: asyncio.Lock = app.state.tick_lock
    if lock.locked():
        return {"skipped": "틱 실행 중 — 중복 호출 직렬화(스킵)"}
    async with lock:
        return await _execute_tick_locked(app)


async def _execute_tick_locked(app: FastAPI) -> dict:
    settings = app.state.settings
    svc: OrderService = app.state.order_service
    toss = app.state.toss_client
    repo = app.state.repo
    if toss is None:
        return {"error": "토스 자격증명 미설정 — 틱 불가"}
    now = datetime.now(KST)

    # 유니버스: 워치리스트 우선 + 2단계 선정(ADV 상위 풀 활용 + 미측정 탐색 — 통계 없으면
    # 순수 코호트 로테이션과 동등). 통계는 틱이 받은 캔들에서 공짜 축적(추가 API 콜 없음).
    watch = [s.strip() for s in (settings.watchlist or "").split(",") if s.strip()]
    if settings.symbol_source_path:
        seed_codes = [e.code for e in
                      await FileSymbolSource(settings.symbol_source_path).symbols()]
        tick_count, adv_pool, fresh = 0, [], set()
        if repo is not None:
            tick_count = await repo.count_ticks()
            adv_pool = await repo.load_adv_pool(settings.adv_pool_size)
            # 신선도 컷: 14일(≈10 거래일) 이전 측정은 낡음 → 탐색 대상으로 재편입
            cutoff = trade_date_kst(now - timedelta(days=14))
            fresh = await repo.load_fresh_symbols(cutoff)
        watch = resolve_universe(
            seed_codes, limit=settings.universe_max_symbols, include=watch,
            tick_count=tick_count, adv_pool=adv_pool, fresh=fresh,
            explore_ratio=settings.universe_explore_ratio,
        )

    # 판단기 선택 — 일일 LLM 판단 상한 도달 시 그날은 결정적 폴백으로 강등(비용 가드)
    if settings.anthropic_api_key:
        llm_capped = False
        if repo is not None and settings.daily_llm_decision_cap > 0:
            used = await repo.count_decisions_today(trade_date_kst(now))
            llm_capped = used >= settings.daily_llm_decision_cap
        if llm_capped:
            judge, research = DeterministicJudge(), None
            engine = (f"일일 LLM 판단 상한({settings.daily_llm_decision_cap}) 도달 "
                      "→ 결정적 폴백(비용 가드)")
        else:
            judge, research = ClaudeJudge(), WebSearchResearch()
            engine = "claude-fable-5 + web_search"
    else:
        judge, research = DeterministicJudge(), None
        engine = "ANTHROPIC_API_KEY 미설정 → 결정적 폴백(주문 데모용)"

    entry_gate = EntryGate(
        CostConfig(
            commission_rate=settings.cost_commission_rate,
            slippage_rate=settings.cost_slippage_rate,
            sell_tax_rate=settings.cost_sell_tax_rate,
        ),
        EntryGateConfig(
            cost_multiple=settings.entry_cost_multiple,
            move_multiple=settings.entry_move_multiple,
        ),
    )

    # 레짐 필터(REGIME_SYMBOL 빈 값이면 비활성)
    regime_config = None
    if settings.regime_symbol:
        regime_config = RegimeConfig(
            symbol=settings.regime_symbol,
            calm_vol=settings.regime_calm_vol,
            stress_vol=settings.regime_stress_vol,
            elevated_multiplier=settings.regime_elevated_multiplier,
            stress_multiplier=settings.regime_stress_multiplier,
        )

    # 영속화 설정 시: 오늘 매수 사용액을 DB에서 읽어 일일 한도를 틱 경계 너머로 강제하고,
    # 틱 전에 리컨실(포지션 대조) — LIVE 불일치면 킬스위치가 걸린 채 틱이 돌아 주문이 차단된다.
    daily_used = Decimal(0)
    holdings = None
    reconcile_report = None
    if repo is not None:
        daily_used = await repo.buy_notional_today(trade_date_kst(now))
        holdings = await toss.get_holdings()
        reconcile_report = await reconcile_and_enforce(
            repo, svc, holdings, now, advance_baseline=True,
            notifier=app.state.notifier, alert_gate=app.state.alert_gate)

    # 페이퍼 모드(DRY_RUN + DB + seed>0): 페이퍼 장부가 파이프라인을 구동한다 — LLM 이 페이퍼
    # 보유를 매도 평가하고 사이징이 페이퍼 현금을 쓰는 자기일관 루프(전략 P&L 측정 목적).
    # 리컨실은 위에서 실계좌 기준으로 이미 수행(분리된 관심사).
    paper = None
    marks: dict[str, Decimal] = {}
    bench_price = None
    tick_holdings, tick_cash = holdings, None
    if repo is not None and svc.mode is TradingMode.DRY_RUN and settings.paper_seed_krw > 0:
        paper = await repo.load_paper()
        if paper is None:
            paper = PaperPortfolio(cash=settings.paper_seed_krw)
            await repo.save_paper(paper, seed=settings.paper_seed_krw)
            await repo.audit("system", "paper_init", {"seed": str(settings.paper_seed_krw)})
        mark_symbols = sorted(
            set(paper.positions) | ({settings.regime_symbol} if settings.regime_symbol else set()))
        if mark_symbols:
            try:  # 배치 1콜: 페이퍼 포지션 마킹 + 벤치마크(시장 프록시). 실패 시 취득가 폴백
                marks = {p.symbol: p.last_price for p in await toss.get_prices(mark_symbols)}
            except Exception:
                marks = {}
            bench_price = marks.get(settings.regime_symbol) if settings.regime_symbol else None
        tick_holdings, tick_cash = paper.to_synthetic_holdings(marks), paper.cash

    # 결정적 청산(손절·타임스톱) — 페이퍼 장부 기준 판정, run_tick 이 LLM 우회 SELL 생성
    forced_exits = None
    if paper is not None and settings.exit_rules_enabled and paper.positions:
        days_held: dict[str, int] = {}
        for sym, pos in paper.positions.items():
            if pos.opened_at is not None:
                days_held[sym] = await repo.count_trading_days_since(
                    trade_date_kst(pos.opened_at))
        forced_exits = evaluate_exits(
            paper.positions, marks, days_held,
            ExitConfig(stop_loss_rate=settings.exit_stop_loss_rate,
                       time_stop_days=settings.exit_time_stop_days))

    # 캔들 TTL 캐시(429 방어) — run_tick 의 get_candles(스크리너·레짐 프록시)만 캐시 대상.
    # 리컨실 holdings·페이퍼 마킹 prices 는 위에서 원본 toss 로 이미 수행(실시간성 유지).
    toss_for_tick = toss
    if repo is not None and settings.candle_cache_ttl_minutes > 0:
        toss_for_tick = CachingToss(toss, repo, settings.candle_cache_ttl_minutes)

    cb_was_tripped = svc.circuit_breaker.tripped               # 알림용 전이 감지(스팸 방지)

    result = await run_tick(
        toss=toss_for_tick, order_service=svc, watchlist=watch, judge=judge, research=research,
        now=now, research_top_n=settings.research_top_n, entry_gate=entry_gate,
        daily_buy_used_krw=daily_used, regime_config=regime_config, holdings=tick_holdings,
        cash_buying_power_krw=tick_cash, max_buy_candidates=settings.judge_top_n,
        forced_exits=forced_exits,
    )

    if svc.circuit_breaker.tripped != cb_was_tripped:          # 전이만 통지
        state = "발동" if svc.circuit_breaker.tripped else "해제"
        await app.state.notifier.send(
            f"⚠️ 서킷브레이커 {state}: {svc.circuit_breaker.reason or '낙폭 회복'}")

    tick_id = None
    paper_summary = None
    if repo is not None:  # 틱/결정/주문 전수 기록 + 엔진 상태(서킷브레이커) 저장
        tick_id = await repo.record_tick(result, started_at=now)
        await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())
        await repo.upsert_symbol_stats(result.adv20, trade_date_kst(now))   # ADV20 축적
        if paper is not None:  # 의도 주문 모의 체결 → 장부 저장 → 자산곡선 1점 기록
            fills = []
            for o in result.orders:
                if o.status is not OrderStatus.DRY_RUN:
                    continue
                f = paper.apply_fill(o.request, entry_gate.cost, now=now)
                if f is None:
                    continue
                fills.append(f.as_dict())
                if not f.skipped and o.request.price is not None:
                    marks.setdefault(o.request.symbol, o.request.price)   # 신규 매수분 마킹가
            await repo.save_paper(paper)
            equity, positions_value = paper.mark_equity(marks)
            await repo.append_paper_equity(now, equity, paper.cash, positions_value,
                                           paper.realized_cum, bench_price)
            paper_summary = {"equity": str(equity), "cash": str(paper.cash),
                             "realized_cum": str(paper.realized_cum),
                             "trade_count": paper.trade_count, "fills": fills}

    return {
        "tick_id": tick_id,
        "mode": result.mode,
        "kill_switch": result.kill_switch,
        "circuit_breaker": result.circuit_breaker,
        "circuit_breaker_reason": result.circuit_breaker_reason,
        "engine": engine,
        "universe_symbols": result.universe_symbols,
        "candidates": result.candidates,
        "cost_gated": result.cost_gated,
        "regime": result.regime,
        "forced_exits": result.forced_exits,
        "reconcile": reconcile_report,
        "paper": paper_summary,
        "decisions": [d.model_dump() for d in result.decisions],
        "orders": result.orders,
        "note": result.note,
    }


def in_market_hours(now: datetime, svc: OrderService) -> bool:
    cfg = svc.config
    n = now.astimezone(KST)
    if n.weekday() >= 5 or n.date().isoformat() in cfg.holidays:   # 주말·KRX 공휴일
        return False
    return cfg.market_open <= n.time() <= cfg.market_close


async def tick_loop(app: FastAPI) -> None:
    """내장 틱 루프 — tick_interval_sec 간격, 장중에만 실행(enforce_market_hours=False 면 항상)."""
    interval = app.state.settings.tick_interval_sec
    logger.info("내장 틱 루프 시작 — %ds 간격(KST 장중에만 실행)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            if app.state.toss_client is None:
                continue                                        # 자격증명 없음 — 대기만
            svc: OrderService = app.state.order_service
            now = datetime.now(KST)
            if svc.config.enforce_market_hours and not in_market_hours(now, svc):
                from app.api.report import maybe_generate_report   # 순환 import 회피(지연)

                await maybe_generate_report(app, now)           # 휴장일 자동 보고서(중복 방지 내장)
                continue                                        # 장외 — LLM/API 비용 절약
            result = await execute_tick(app)
            logger.info("자동 틱 완료: tick_id=%s candidates=%s note=%s",
                        result.get("tick_id"), result.get("candidates"),
                        result.get("note") or result.get("skipped") or "")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("자동 틱 실패 — 다음 주기에 재시도")
            if app.state.alert_gate.allow(f"loop:{type(e).__name__}"):   # 같은 예외 60분 1회
                await app.state.notifier.send(f"❌ 자동 틱 실패: {type(e).__name__}: {e}")
