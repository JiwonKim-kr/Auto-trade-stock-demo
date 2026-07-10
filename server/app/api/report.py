"""보고서 경계 조립 — DB 조회 → 렌더 → DB 본문 저장(정본) → 파일(best-effort) → 텔레그램 (PLAN §7.2·§3.9).

트리거 2경로(시맨틱 동일 = scheduled_report): 내장 루프의 휴장일 분기(로컬) ·
Scheduler 잡 ②의 POST /internal/report?force=false(클라우드 — 거래일/기생성은 스킵).
수동 즉시 생성은 ?force=true. 페이퍼 데이터(DB) 없으면 스킵.
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

    out_path = ""
    try:   # 파일은 best-effort — Cloud Run FS 는 휘발·비루트 쓰기 불가일 수 있다(§3.9)
        out_dir = Path(app.state.settings.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"report-{period_end}.md"
        p.write_text(text, encoding="utf-8")
        out_path = str(p)
    except OSError as e:
        logger.warning("보고서 파일 저장 실패(DB 본문이 정본): %s", e)
    await repo.record_report(period_end, out_path, body=text)
    await app.state.notifier.send(summary_line(period_end, eval_report, equity_rows))
    logger.info("보고서 생성: period_end=%s file=%s", period_end, out_path or "-")
    return {"path": out_path or None, "period_end": period_end}


async def scheduled_report(app: FastAPI, now: datetime) -> dict:
    """휴장일(주말·공휴일)에만 실생성 — 내장 루프·Scheduler 잡 ② 공용 시맨틱."""
    if app.state.repo is None:
        return {"skipped": "DATABASE_URL 미설정 — 보고서는 페이퍼 이력(DB) 필요"}
    if is_trading_day(now.date(), app.state.holidays):
        return {"skipped": "거래일 — 보고서는 휴장일에 생성"}
    return await generate_report(app, force=False)


async def maybe_generate_report(app: FastAPI, now: datetime) -> None:
    """내장 루프의 장외 분기용 — 예외는 삼킨다(루프 보호)."""
    try:
        result = await scheduled_report(app, now)
        if "period_end" in result:
            logger.info("휴장일 자동 보고서: %s", result["period_end"])
    except Exception:
        logger.exception("자동 보고서 생성 실패")
