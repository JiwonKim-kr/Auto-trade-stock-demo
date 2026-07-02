"""HTTP 라우트.

공개:        GET /health                       (Cloud Run 헬스체크)
인증(API키): GET  /api/status                  현황(모드/킬스위치/장시간/가드레일)
            GET  /api/holdings                토스 보유 프록시
            GET  /api/buying-power            매수가능금액 프록시
            GET  /api/prices?symbols=A,B      현재가 프록시
            POST /api/kill-switch             킬스위치 토글
            GET  /api/orders                  주문 원장(의도/전송 결과)
            POST /internal/tick               거래 틱(전 파이프라인, DRY_RUN). 운영은 OIDC 권장(TODO)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.deps import get_order_service, get_toss_client, require_api_key
from app.engine.costs import CostConfig, EntryGate, EntryGateConfig
from app.engine.llm import ClaudeJudge
from app.engine.pipeline import DeterministicJudge, run_tick
from app.engine.research import WebSearchResearch
from app.engine.symbols import FileSymbolSource, resolve_symbols
from app.orders.guardrails import KST
from app.orders.service import OrderService
from app.toss.client import TossClient

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
async def kill_switch(body: KillSwitchBody, svc: OrderService = Depends(get_order_service)) -> dict:
    if body.engaged:
        svc.engage_kill_switch()
    else:
        svc.release_kill_switch()
    return {"kill_switch": svc.kill_switch}


@api.get("/orders")
async def orders(svc: OrderService = Depends(get_order_service)):
    return svc.ledger


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

    result = await run_tick(
        toss=toss, order_service=svc, watchlist=watch, judge=judge, research=research,
        now=datetime.now(KST), research_top_n=settings.research_top_n, entry_gate=entry_gate,
    )
    return {
        "mode": result.mode,
        "kill_switch": result.kill_switch,
        "circuit_breaker": result.circuit_breaker,
        "circuit_breaker_reason": result.circuit_breaker_reason,
        "engine": engine,
        "universe_symbols": result.universe_symbols,
        "candidates": result.candidates,
        "cost_gated": result.cost_gated,
        "decisions": [d.model_dump() for d in result.decisions],
        "orders": result.orders,
        "note": result.note,
    }


router.include_router(api)
