"""샌드박스 상시 운용 런처 — 합성 시세로 거래 틱을 계속 돌리고 대시보드로 관측.

실계좌·비용과 완전 분리된다:
  - 토스 API 호출 0 (SandboxToss 합성 시세)
  - LLM 호출 0 (기본값 — ANTHROPIC_API_KEY 를 주입하지 않아 결정적 폴백 판단기 사용).
    실제 LLM 판단을 보고 싶으면 `--llm` (유료! 틱마다 조사·판단 호출)
  - **별도 sqlite DB**(sandbox.db) — 운영 Supabase 의 페이퍼 원장·논문 뉴스를 건드리지 않는다
  - 장시간 무시(ENFORCE_MARKET_HOURS=false) → 밤에도 틱이 돈다

실행:
  server/.venv/Scripts/python server/scripts/run_sandbox.py            # 30초 틱, 무료
  server/.venv/Scripts/python server/scripts/run_sandbox.py --interval 10 --seed 7
  server/.venv/Scripts/python server/scripts/run_sandbox.py --llm      # LLM 판단(유료)
  → 대시보드 http://127.0.0.1:8010/dashboard  (API 키 = 아래 SANDBOX_API_KEY)

초기화: server/sandbox.db 를 지우면 페이퍼 장부·틱 이력이 리셋된다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SERVER_DIR = Path(__file__).resolve().parents[1]
SANDBOX_API_KEY = "sandbox-local-key"          # 로컬 전용(합성 데이터라 비밀 가치 없음)


def arg(name: str, default: str) -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main() -> None:
    interval = arg("--interval", "30")
    seed = arg("--seed", "42")
    port = int(arg("--port", "8010"))
    host = arg("--host", "127.0.0.1")

    # 샌드박스 전용 env — .env 는 읽지 않는다(운영 자격증명·DB 가 새어들지 않게)
    env = {
        "SANDBOX_MODE": "true",
        "SANDBOX_SEED": seed,
        "SANDBOX_DAY_SECONDS": interval,        # 틱 1회 = 시뮬 1일
        "DATABASE_URL": "sqlite+aiosqlite:///./sandbox.db",
        "TICK_INTERVAL_SEC": interval,
        "ENFORCE_MARKET_HOURS": "false",        # 장외에도 계속 돌게
        "SYMBOL_SOURCE_PATH": "data/krx_symbols.json",
        "API_KEY": SANDBOX_API_KEY,
        "PAPER_SEED_KRW": arg("--seed-krw", "10000000"),
        "UNIVERSE_MAX_SYMBOLS": arg("--universe", "20"),
        "TRADING_MODE": "DRY_RUN",
    }
    if "--llm" in sys.argv:                     # 유료 — 명시적 opt-in
        for k in ("ANTHROPIC_API_KEY",):
            for p in (SERVER_DIR / "scripts" / ".env", SERVER_DIR / ".env"):
                if p.is_file():
                    for ln in p.read_text(encoding="utf-8").splitlines():
                        if ln.strip().startswith(k + "="):
                            env[k] = ln.partition("=")[2].strip().strip('"').strip("'")
                    break
        if not env.get("ANTHROPIC_API_KEY"):
            print("[경고] --llm 인데 ANTHROPIC_API_KEY 를 .env 에서 못 찾음 → 결정적 폴백으로 진행")

    os.environ.update(env)
    os.chdir(SERVER_DIR)
    sys.path.insert(0, str(SERVER_DIR))

    print("🧪 샌드박스 상시 운용")
    print(f"   틱 {interval}s · seed {seed} · DB sandbox.db · 토스 0콜 · "
          f"LLM {'ON(유료)' if env.get('ANTHROPIC_API_KEY') else 'OFF(폴백)'}")
    print(f"   대시보드 http://{'127.0.0.1' if host=='127.0.0.1' else host}:{port}/dashboard"
          f"  (API 키: {SANDBOX_API_KEY})")

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
