"""거래 틱 DRY_RUN 라이브 점검 — 전 파이프라인을 실계좌(읽기 전용)로 1회 실행.

수집(holdings)→유니버스→스크리너→(조사 생략)→판단(결정적 폴백)→사이징→DRY_RUN 주문 까지
실제 토스 데이터로 돈다. ❗실주문은 절대 나가지 않는다(DRY_RUN + 주문층 보장).

실행:
  python server/scripts/tick_dry_run.py 005930,000660   # 명시 워치리스트(기본 005930)
  python server/scripts/tick_dry_run.py --seed [N]       # KRX 시드에서 상위 N(기본 15) 후보
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_env() -> None:
    here = Path(__file__).resolve().parent
    for p in (here / ".env", here.parent / ".env"):
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.pipeline import DeterministicJudge, run_tick  # noqa: E402
from app.engine.symbols import FileSymbolSource, resolve_symbols  # noqa: E402
from app.orders.guardrails import KST  # noqa: E402
from app.orders.models import TradingMode  # noqa: E402
from app.orders.service import OrderService  # noqa: E402
from app.toss.client import TossClient, TossConfig  # noqa: E402


async def resolve_watchlist() -> list[str]:
    """인자 파싱: `--seed [N]` 이면 KRX 시드에서 상위 N, 아니면 쉼표구분 워치리스트(기본 005930)."""
    args = sys.argv[1:]
    if args and args[0] == "--seed":
        cap = int(args[1]) if len(args) > 1 and args[1].isdigit() else 15
        seed = Path(__file__).resolve().parents[1] / "data" / "krx_symbols.json"
        watch = await resolve_symbols(FileSymbolSource(seed), limit=cap)
        print(f"심볼 소스=KRX 시드({seed.name}) · 상한={cap} · 후보 {len(watch)}개")
        return watch
    raw = args[0] if args else "005930"
    return [s.strip() for s in raw.split(",") if s.strip()]


async def main() -> int:
    load_env()
    try:
        cfg = TossConfig.from_env()
    except RuntimeError as e:
        print(f"[중단] {e}")
        return 1
    if cfg.client_id in ("", "your_client_id_here"):
        print("[중단] 토스 자격증명이 채워지지 않았습니다.")
        return 1

    watch = await resolve_watchlist()
    svc = OrderService(mode=TradingMode.DRY_RUN)

    print(f"틱 DRY_RUN 라이브 점검 · 워치리스트={watch} · ❗주문 미전송")
    async with TossClient(cfg) as toss:
        res = await run_tick(toss=toss, order_service=svc, watchlist=watch,
                             judge=DeterministicJudge(), now=datetime.now(KST))

    print(f"\nmode={res.mode}  kill_switch={res.kill_switch}  circuit_breaker={res.circuit_breaker}")
    if res.circuit_breaker:
        print(f"  ⚠️ {res.circuit_breaker_reason}")
    print(f"유니버스(적격)={res.universe_symbols}  후보={res.candidates}")
    print("결정:")
    for d in res.decisions:
        print(f"  {d.symbol}  {d.action.value}  conf={d.confidence:.2f}  · {d.rationale}")
    print("주문(DRY_RUN):")
    if not res.orders:
        print("  (없음)")
    for o in res.orders:
        q = o.request.quantity
        print(f"  {o.request.symbol}  {o.request.side.value}  {q}주  status={o.status.value}"
              + (f"  · {o.reason}" if o.reason else ""))
    if res.note:
        print(f"note: {res.note}")
    print(f"\n❗ 실주문 0 확인: {all(not o.sent_to_market for o in res.orders)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
