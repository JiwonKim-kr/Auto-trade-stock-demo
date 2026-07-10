"""보고서 경계 조립 — DB 조회 → 렌더 → 파일 저장 → 텔레그램 요약 (PLAN §7.2).

트리거 2경로: 내장 루프의 휴장일 분기(maybe_generate_report — 중복 방지) + 수동
POST /internal/report(force). 페이퍼 데이터(DB) 없으면 스킵.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI

from app.core.calendar import is_trading_day
from app.engine.evaluation import evaluate
from app.engine.report import render_report, summary_line

logger = logging.getLogger("app.report")


async def generate_report(app: FastAPI, *, force: bool = False) -> dict:
    repo = app.state.repo
    if repo is None:
        return {"skipped": "DATABASE_URL 미설정 — 보고서는 페이퍼 이력(DB) 필요"}
    equity_rows = await repo.load_daily_equity()
    if not equity_rows:
        return {"skipped": "자산곡선 없음 — 운용 데이터가 쌓인 뒤 생성"}

    period_end = equity_rows[-1][0]
    last = await repo.last_report_period_end()
    if not force and last is not None and last >= period_end:
        return {"skipped": f"이미 보고됨(period_end={last})"}

    paper = await repo.load_paper()
    eval_report = evaluate([(d, e) for d, e, _ in equity_rows],
                           [(d, b) for d, _, b in equity_rows],
                           n_trades=paper.trade_count if paper else 0)
    activity = await repo.load_period_activity(last)
    text = render_report(period_start=last, period_end=period_end, equity_rows=equity_rows,
                         eval_report=eval_report, decisions=activity["decisions"],
                         orders=activity["orders"], audits=activity["audits"],
                         ticks=activity["ticks"])

    out_dir = Path(app.state.settings.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report-{period_end}.md"
    path.write_text(text, encoding="utf-8")
    await repo.record_report(period_end, str(path))
    await app.state.notifier.send(summary_line(period_end, eval_report, equity_rows))
    logger.info("보고서 생성: %s", path)
    return {"path": str(path), "period_end": period_end}


async def maybe_generate_report(app: FastAPI, now: datetime) -> None:
    """휴장일(주말·공휴일)에만, 새 데이터가 있으면 1회 생성 — 내장 루프의 장외 분기에서 호출."""
    if app.state.repo is None or is_trading_day(now.date(), app.state.holidays):
        return
    try:
        result = await generate_report(app, force=False)
        if "path" in result:
            logger.info("휴장일 자동 보고서: %s", result["path"])
    except Exception:
        logger.exception("자동 보고서 생성 실패")
