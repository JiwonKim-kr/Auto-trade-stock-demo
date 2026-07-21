"""GUI 대시보드 — 프로젝트 전체 상태를 브라우저에서 관측(읽기 전용).

설계: 데이터 원천은 DB(repo) — Supabase 든 로컬 sqlite 든 동일. 로컬에서 서버를
DATABASE_URL=Supabase 로 띄우면 클라우드가 하는 걸 그대로 비춘다(틱은 안 돌림 — 뷰어).

경로:
  GET /dashboard                셸 HTML(무인증 — 데이터 없음). JS 가 API 키로 데이터 폴링.
  GET /api/dashboard/overview   전체 스냅샷 1콜(상태·평가·자산곡선·활동·뉴스·안전) — 인증.

제어(킬스위치·틱 등) 확장 대비: overview 는 순수 조회. 이후 제어는 기존 POST 엔드포인트
(/api/kill-switch·/api/circuit-breaker/reset·/internal/tick)를 이 페이지에서 호출하면 된다
— 그래서 데이터/셸을 분리해 뒀다(제어 버튼은 같은 X-API-Key 로 붙는다).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.deps import require_api_key
from app.engine.evaluation import evaluate
from app.orders.guardrails import KST

router = APIRouter()
_PAGE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """대시보드 셸(정적 HTML). 데이터는 JS 가 /api/dashboard/overview 에서 가져온다."""
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"))


@router.get("/api/dashboard/overview", dependencies=[Depends(require_api_key)])
async def overview(request: Request) -> dict:
    """대시보드 전체 스냅샷 1콜. DB 없으면 상태만(운영 데이터 없음)."""
    app = request.app
    svc = app.state.order_service
    repo = app.state.repo
    now_kst = datetime.now(KST)
    cfg = svc.config
    market_open = (now_kst.weekday() < 5
                   and cfg.market_open <= now_kst.time() <= cfg.market_close
                   and now_kst.date().isoformat() not in app.state.holidays)

    status = {
        "mode": svc.mode.value,
        "kill_switch": svc.kill_switch,
        "circuit_breaker": svc.circuit_breaker.snapshot(),
        "market_open_now": market_open,
        "toss_connected": app.state.toss_client is not None,
        "persistence": repo is not None,
        "server_time_kst": now_kst.isoformat(),
        "guardrails": {
            "per_order_max_krw": str(cfg.per_order_max_krw),
            "daily_buy_cap_krw": str(cfg.daily_buy_cap_krw),
            "max_positions": cfg.max_positions,
            "per_symbol_max_weight": str(cfg.per_symbol_max_weight),
        },
    }
    if repo is None:
        return {"status": status, "persistence": False}

    equity_rows = await repo.load_daily_equity()
    paper = await repo.load_paper()
    evaluation = evaluate([(d, e) for d, e, _ in equity_rows],
                          [(d, b) for d, _, b in equity_rows],
                          n_trades=paper.trade_count if paper else 0).as_dict()
    return {
        "status": status,
        "persistence": True,
        "evaluation": evaluation,
        "equity": [{"date": d, "equity": str(e), "benchmark": (str(b) if b is not None else None)}
                   for d, e, b in equity_rows],
        "paper": ({"cash": str(paper.cash), "realized": str(paper.realized_cum),
                   "trade_count": paper.trade_count, "positions": len(paper.positions)}
                  if paper else None),
        "ticks": await repo.recent_ticks(20),
        "orders": await repo.recent_orders(20),
        "decisions": await repo.recent_decisions(30),
        "audits": await repo.recent_audits(20),
        "news": await repo.news_stats(),
    }
