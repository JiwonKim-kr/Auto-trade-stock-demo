"""토스 클라이언트 라이브 점검 — 운영 코드(httpx + Pydantic 모델) 경로로 읽기 전용 확인.

스모크(stdlib 원시 덤프, toss_smoke.py)와 달리 실제 TossClient 로 토큰·계좌·보유·시세를
조회해 **프로덕션 경로가 라이브에서 동작**하는지 확인한다. ❗주문(POST /orders)은 호출하지 않는다.

실행: python server/scripts/live_check.py    (server/scripts/.env 의 자격증명 사용)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_dotenv() -> Path | None:
    here = Path(__file__).resolve().parent
    for p in (here / ".env", here.parent / ".env", Path.cwd() / ".env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return p
    return None


# app 패키지 import 경로 (server/)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.toss.client import TossAPIError, TossClient, TossConfig  # noqa: E402


async def main() -> int:
    env_path = load_dotenv()
    print(f".env: {env_path or '없음'}")
    try:
        cfg = TossConfig.from_env()
    except RuntimeError as e:
        print(f"[중단] {e}")
        return 1
    if cfg.client_id in ("", "your_client_id_here"):
        print("[중단] 자격증명이 아직 채워지지 않았습니다.")
        return 1

    async with TossClient(cfg) as c:
        accounts = await c.get_accounts()
        print(f"✅ accounts        : {len(accounts)}개  accountSeq={accounts[0].account_seq}")

        h = await c.get_holdings()
        print(f"✅ holdings        : items={len(h.items)}  currencies={[i.currency for i in h.items]}")

        bp = await c.get_buying_power("KRW")
        print(f"✅ buying-power KRW : {bp.cash_buying_power}")

        prices = await c.get_prices(["005930"])
        print(f"✅ price 005930     : {prices[0].last_price} {prices[0].currency}")

    print("점검 완료. ❗ 주문 미호출(읽기 전용).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except TossAPIError as e:
        print(f"[토스 에러] {e}")
        raise SystemExit(1)
