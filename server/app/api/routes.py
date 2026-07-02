"""HTTP 라우트.

공개:        GET /health                       (Cloud Run 헬스체크)
인증(API키): GET  /api/status                  현황(모드/킬스위치/장시간/가드레일)
            GET  /api/holdings                토스 보유 프록시
            GET  /api/buying-power            매수가능금액 프록시
            GET  /api/prices?symbols=A,B      현재가 프록시
            POST /api/kill-switch             킬스위치 토글
            GET  /api/orders                  주문 원장(의도/전송 결과)
            GET  /api/reconcile               리컨실 수동 점검(기준선 미이동 — DB 필요)
            POST /internal/tick               거래 틱(전 파이프라인, DRY_RUN). 운영은 OIDC 권장(TODO)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.deps import get_order_service, get_toss_client, require_api_key
from app.db.repo import trade_date_kst
from app.engine.costs import CostConfig, EntryGate, EntryGateConfig
from app.engine.llm import ClaudeJudge
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.regime import RegimeConfig
from app.engine.research import WebSearchResearch
from app.engine.symbols import FileSymbolSource, resolve_symbols
from app.orders.guardrails import KST
from app.orders.models import TradingMode
from app.orders.reconcile import reconcile, snapshot_from_holdings
from app.orders.service import OrderService
from app.toss.client import TossClient
from app.toss.models import Holdings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


@api.get("/status")
async def status_(request: Request, svc: OrderService = Depends(get_order_service)) -> dict:
    now_kst = datetime.now(KST)
    cfg = svc.config
    market_open = (
        now_kst.weekday() < 5 and cfg.market_open <= now_kst.time() <= cfg.market_close
    )
    return {
        "mode": svc.mode.value,
        "kill_switch": svc.kill_switch,
        "circuit_breaker": svc.circuit_breaker.snapshot(),
        "market_open_now": market_open,
        "toss_connected": request.app.state.toss_client is not None,
        "persistence": request.app.state.repo is not None,
        "guardrails": {
            "per_order_max_krw": str(cfg.per_order_max_krw),
            "daily_buy_cap_krw": str(cfg.daily_buy_cap_krw),
            "max_positions": cfg.max_positions,
            "per_symbol_max_weight": str(cfg.per_symbol_max_weight),
            "enforce_market_hours": cfg.enforce_market_hours,
        },
        "orders_in_ledger": len(svc.ledger),
    }


@api.get("/holdings")
async def holdings(toss: TossClient = Depends(get_toss_client)):
    return await toss.get_holdings()


@api.get("/buying-power")
async def buying_power(currency: str = "KRW", toss: TossClient = Depends(get_toss_client)):
    return await toss.get_buying_power(currency)


@api.get("/prices")
async def prices(
    symbols: str = Query(..., description="쉼표 구분 종목코드 (예: 005930,000660)"),
    toss: TossClient = Depends(get_toss_client),
):
    return await toss.get_prices(symbols)


class KillSwitchBody(BaseModel):
    engaged: bool


@api.post("/kill-switch")
async def kill_switch(
    body: KillSwitchBody, request: Request, svc: OrderService = Depends(get_order_service)
) -> dict:
    if body.engaged:
        svc.engage_kill_switch()
    else:
        svc.release_kill_switch()
    repo = request.app.state.repo
    if repo is not None:  # 재시작 생존 + 감사
        await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())
        await repo.audit("api", "kill_switch", {"engaged": svc.kill_switch})
    return {"kill_switch": svc.kill_switch}


@api.get("/orders")
async def orders(svc: OrderService = Depends(get_order_service)):
    return svc.ledger


async def _reconcile_and_enforce(
    repo, svc: OrderService, holdings: Holdings, now: datetime, *, advance_baseline: bool
) -> dict:
    """리컨실 실행 + 집행: 불일치 감사 기록, LIVE 면 킬스위치 자동 발동(거래 중단).

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
        if svc.mode is TradingMode.LIVE and not svc.kill_switch:
            svc.engage_kill_switch()                             # 실자금 위 불일치 → 거래 중단
            await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())
            await repo.audit("system", "kill_switch",
                             {"engaged": True, "cause": "reconcile_mismatch"})
    if advance_baseline:
        await repo.save_positions_snapshot(now, items)
    return report.as_dict()


@api.get("/reconcile")
async def reconcile_check(
    request: Request,
    svc: OrderService = Depends(get_order_service),
    toss: TossClient = Depends(get_toss_client),
) -> dict:
    """수동 리컨실 점검(기준선 미이동). 불일치 시 감사 기록, LIVE 면 킬스위치 발동."""
    repo = request.app.state.repo
    if repo is None:
        return {"status": "DISABLED", "reason": "DATABASE_URL 미설정 — 리컨실은 DB 필요"}
    holdings = await toss.get_holdings()
    return await _reconcile_and_enforce(repo, svc, holdings, datetime.now(KST),
                                        advance_baseline=False)


@router.post("/internal/tick", dependencies=[Depends(require_api_key)])
async def tick(
    request: Request,
    svc: OrderService = Depends(get_order_service),
    toss: TossClient = Depends(get_toss_client),
) -> dict:
    """거래 틱: 수집→유니버스→스크리너→조사→판단→사이징→DRY_RUN 주문. 운영은 OIDC 권장(TODO)."""
    settings = request.app.state.settings
    watch = [s.strip() for s in (settings.watchlist or "").split(",") if s.strip()]

    # 심볼 소스 설정 시: KRX 시드 ∪ 워치리스트(우선) → 후보 상한 적용. 미설정이면 워치리스트만(기존 동작).
    if settings.symbol_source_path:
        watch = await resolve_symbols(
            FileSymbolSource(settings.symbol_source_path),
            limit=settings.universe_max_symbols,
            include=watch,
        )

    if settings.anthropic_api_key:
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
    now = datetime.now(KST)
    repo = request.app.state.repo
    daily_used = Decimal(0)
    holdings = None
    reconcile_report = None
    if repo is not None:
        daily_used = await repo.buy_notional_today(trade_date_kst(now))
        holdings = await toss.get_holdings()
        reconcile_report = await _reconcile_and_enforce(repo, svc, holdings, now,
                                                        advance_baseline=True)

    result = await run_tick(
        toss=toss, order_service=svc, watchlist=watch, judge=judge, research=research,
        now=now, research_top_n=settings.research_top_n, entry_gate=entry_gate,
        daily_buy_used_krw=daily_used, regime_config=regime_config, holdings=holdings,
    )

    tick_id = None
    if repo is not None:  # 틱/결정/주문 전수 기록 + 엔진 상태(서킷브레이커) 저장
        tick_id = await repo.record_tick(result, started_at=now)
        await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())

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
        "reconcile": reconcile_report,
        "decisions": [d.model_dump() for d in result.decisions],
        "orders": result.orders,
        "note": result.note,
    }


router.include_router(api)
