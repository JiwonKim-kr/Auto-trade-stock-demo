"""HTTP 라우트.

공개:        GET /health                       (Cloud Run 헬스체크)
인증(API키): GET  /api/status                  현황(모드/킬스위치/장시간/가드레일)
            GET  /api/holdings                토스 보유 프록시
            GET  /api/buying-power            매수가능금액 프록시
            GET  /api/prices?symbols=A,B      현재가 프록시
            POST /api/kill-switch             킬스위치 토글
            POST /api/circuit-breaker/reset   서킷브레이커 수동 리셋(입출금 왜곡 해소 — §1.3)
            GET  /api/orders                  주문 원장(의도/전송 결과)
            GET  /api/reconcile               리컨실 수동 점검(기준선 미이동 — DB 필요)
            GET  /api/evaluation              페이퍼 성과 평가(Sharpe/MDD/벤치마크·표본 게이트 — DB 필요)
            POST /internal/tick               거래 틱(조립은 api/tick.py). 운영은 OIDC 권장(TODO)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.deps import get_order_service, get_toss_client, require_api_key
from app.api.report import generate_report
from app.api.tick import execute_tick, reconcile_and_enforce
from app.engine.evaluation import evaluate
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
    await request.app.state.notifier.send(
        f"킬스위치 {'ON — 전 주문 차단' if svc.kill_switch else 'OFF — 재개'} (수동)")
    return {"kill_switch": svc.kill_switch}


@api.post("/circuit-breaker/reset")
async def circuit_breaker_reset(
    request: Request, svc: OrderService = Depends(get_order_service)
) -> dict:
    """서킷브레이커 수동 리셋(§1.3) — 입출금으로 왜곡된 HWM·래치 초기화.

    손실 조건이 여전하면 다음 틱 assess 가 즉시 재발동한다(리셋이 실손실을 가리지 못함).
    """
    before = svc.circuit_breaker.snapshot()
    svc.circuit_breaker.reset()
    repo = request.app.state.repo
    if repo is not None:  # 재시작 생존 + 감사
        await repo.save_engine_state(svc.kill_switch, svc.circuit_breaker.dump_state())
        await repo.audit("api", "circuit_breaker_reset", {"before": before})
    await request.app.state.notifier.send(
        "서킷브레이커 수동 리셋 — HWM·래치 초기화(다음 틱 재평가)")
    return {"circuit_breaker": svc.circuit_breaker.snapshot(), "before": before}


@api.get("/orders")
async def orders(svc: OrderService = Depends(get_order_service)):
    return svc.ledger


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
    holdings_ = await toss.get_holdings()
    return await reconcile_and_enforce(repo, svc, holdings_, datetime.now(KST),
                                       advance_baseline=False,
                                       notifier=request.app.state.notifier,
                                       alert_gate=request.app.state.alert_gate)


@api.get("/evaluation")
async def evaluation_check(request: Request) -> dict:
    """페이퍼 자산곡선 → Sharpe/MDD/벤치마크 대비 + 표본 게이트(N<100 판단 보류)."""
    repo = request.app.state.repo
    if repo is None:
        return {"status": "DISABLED", "reason": "DATABASE_URL 미설정 — 평가는 페이퍼 장부(DB) 필요"}
    rows = await repo.load_daily_equity()
    paper = await repo.load_paper()
    report = evaluate([(d, e) for d, e, _ in rows], [(d, b) for d, _, b in rows],
                      n_trades=paper.trade_count if paper else 0)
    return report.as_dict()


@router.post("/internal/tick", dependencies=[Depends(require_api_key)])
async def tick(request: Request, toss: TossClient = Depends(get_toss_client)) -> dict:
    """거래 틱 1회(조립·실행은 api/tick.py — 내장 루프와 공유). 운영은 OIDC 권장(TODO)."""
    return await execute_tick(request.app)


@router.post("/internal/report", dependencies=[Depends(require_api_key)])
async def report_now(request: Request) -> dict:
    """운용 보고서 수동 생성(중복 방지 무시). 자동은 내장 루프가 휴장일에 1회."""
    return await generate_report(request.app, force=True)


router.include_router(api)
