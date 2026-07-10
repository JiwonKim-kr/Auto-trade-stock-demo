"""운용 보고서 렌더러 — 휴장일마다 기간 요약 마크다운 생성 (PLAN §7.2). 순수 함수(조회는 경계가).

내용: 자산곡선(기간 수익·벤치마크) · 누적 평가(evaluate 재사용) · 기간 판단/주문 통계 ·
강제청산 목록 · 차단 통계(비용게이트·레짐) · 안전 이벤트(감사로그).
"""

from __future__ import annotations

import json
from decimal import Decimal

from app.engine.evaluation import EvalReport


def _pct(x: float | None) -> str:
    return "N/A" if x is None else f"{x * 100:+.2f}%"


def render_report(
    *,
    period_start: str | None,
    period_end: str,
    equity_rows: list[tuple[str, Decimal, Decimal | None]],   # (date, equity, benchmark)
    eval_report: EvalReport,
    decisions: list[dict],        # {action, symbol, confidence, rationale}
    orders: list[dict],           # {side, symbol, quantity, price, status}
    audits: list[dict],           # {ts, actor, action}
    ticks: list[dict],            # {cost_gated_json, regime_json}
) -> str:
    period = f"{period_start or '운용 시작'} ~ {period_end}"
    lines = [f"# 페이퍼 운용 보고서 — {period}", ""]

    # 자산
    in_period = [r for r in equity_rows if period_start is None or r[0] > period_start]
    if in_period:
        start_eq = float(in_period[0][1])
        end_eq = float(in_period[-1][1])
        ret = end_eq / start_eq - 1.0 if start_eq > 0 else None
        bench_pts = [float(b) for _, _, b in in_period if b is not None]
        bench = bench_pts[-1] / bench_pts[0] - 1.0 if len(bench_pts) >= 2 else None
        lines += ["## 자산 (기간)",
                  f"- 자산: {start_eq:,.0f} → {end_eq:,.0f} KRW ({_pct(ret)})",
                  f"- 벤치마크(시장 프록시): {_pct(bench)}", ""]

    # 누적 평가
    d = eval_report.as_dict()
    sharpe = f"{d['sharpe_annual']:.2f}" if d["sharpe_annual"] is not None else "N/A"
    se = f"{d['sharpe_se_annual']:.2f}" if d["sharpe_se_annual"] is not None else "N/A"
    lines += ["## 누적 평가 (운용 전체)",
              f"- 누적수익 {_pct(d['cumulative_return'])} · MDD {_pct(d['mdd'])} · "
              f"Sharpe(연) {sharpe} ± {se}",
              f"- 완결 트레이드 N={d['n_trades']} · 판정: {d['verdict']}", ""]

    # 판단/주문
    by_action: dict[str, int] = {}
    for dec in decisions:
        by_action[dec["action"]] = by_action.get(dec["action"], 0) + 1
    forced = [dec for dec in decisions if "결정적 청산" in (dec.get("rationale") or "")]
    by_status: dict[str, int] = {}
    for o in orders:
        by_status[o["status"]] = by_status.get(o["status"], 0) + 1
    lines += ["## 판단 · 주문 (기간)",
              f"- 판단: {json.dumps(by_action, ensure_ascii=False)}",
              f"- 주문: {json.dumps(by_status, ensure_ascii=False)}"]
    if forced:
        lines.append("- 강제 청산:")
        lines += [f"  - {f_['symbol']}: {f_['rationale']}" for f_ in forced]
    lines.append("")

    # 차단 통계
    gated = sum(len(json.loads(t.get("cost_gated_json") or "[]")) for t in ticks)
    regimes: dict[str, int] = {}
    for t in ticks:
        lvl = (json.loads(t.get("regime_json") or "{}")).get("level")
        if lvl:
            regimes[lvl] = regimes.get(lvl, 0) + 1
    lines += ["## 차단 · 레짐 (기간)",
              f"- 비용 게이트 차단 매수 후보: {gated}건",
              f"- 레짐 분포(틱 수): {json.dumps(regimes, ensure_ascii=False) or '없음'}", ""]

    # 안전 이벤트
    lines.append("## 안전 이벤트 (기간)")
    if audits:
        lines += [f"- {a['ts']}: [{a['actor']}] {a['action']}" for a in audits]
    else:
        lines.append("- 없음")
    lines += ["", "> 캘리브레이션 상세: `scripts/calibration_report.py` · 평가 API: `GET /api/evaluation`"]
    return "\n".join(lines)


def summary_line(period_end: str, eval_report: EvalReport,
                 equity_rows: list[tuple[str, Decimal, Decimal | None]]) -> str:
    """텔레그램용 1줄 요약(4,096자 제한 대비 — 핵심만)."""
    d = eval_report.as_dict()
    eq = f"{float(equity_rows[-1][1]):,.0f}" if equity_rows else "N/A"
    return (f"📊 보고서({period_end}) 자산 {eq} KRW · 누적 {_pct(d['cumulative_return'])} · "
            f"MDD {_pct(d['mdd'])} · N={d['n_trades']}")
