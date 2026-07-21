"""로컬 서버 런처 — scripts/.env 를 process env 로 로드한 뒤 uvicorn 구동.

서버 설정(pydantic-settings)은 **process env 만** 읽는다(.env 자동 로드 없음 — 파일 위치에 따라
몰래 LIVE 가 켜지는 사고를 막는 명시성). 이 런처가 scripts/.env(자격증명·설정)를 os.environ 에
올리고(이미 있는 env 가 우선) 서버를 켠다. TRADING_MODE=LIVE 다중확인도 같은 경로로 일관 동작.

작업폴더를 server/ 로 고정한다 → DATABASE_URL=sqlite+aiosqlite:///./trading.db 는 항상
server/trading.db (gitignore: *.db). 운영(Cloud Run)은 Secret Manager → env 주입이라 미사용.

실행: server/.venv/Scripts/python server/scripts/run_local.py [--port 8000] [--host 0.0.0.0]
대시보드: 기동 후 http://127.0.0.1:8000/dashboard (API 키 = .env 의 API_KEY).
모바일(같은 WiFi): `--host 0.0.0.0` 로 띄우고 폰에서 http://<PC-LAN-IP>:8000/dashboard 접속.
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


def load_env() -> None:
    for p in (SERVER_DIR / "scripts" / ".env", SERVER_DIR / ".env"):
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, _, v = ln.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def main() -> None:
    load_env()
    os.chdir(SERVER_DIR)                       # 상대경로(DB 등) 기준 고정
    sys.path.insert(0, str(SERVER_DIR))
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
