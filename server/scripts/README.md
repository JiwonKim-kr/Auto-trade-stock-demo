# 진단/점검 스크립트

모두 **읽기 전용 또는 DRY_RUN** — 주문(`POST /api/v1/orders` 계열)은 절대 전송하지 않는다.
자격증명은 이 폴더의 `.env`(또는 `server/.env`)에서 읽는다. `.env` 는 `.gitignore` 로 커밋 차단 —
**절대 공유/커밋 금지**(실자금 자격증명).

```powershell
# 자격증명 준비 (토스증권 PC 웹 → 설정 → Open API)
Copy-Item server/scripts/.env.example server/scripts/.env
#   → .env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 채우기
```

| 스크립트 | 용도 | 의존성 |
|---|---|---|
| `toss_smoke.py` | 토스 실응답 **원시 덤프** + `accountSeq` 자동 판별(인사이트 §4). 필드 확정용 첫 코드 | 없음(stdlib) |
| `probe_candles.py` | `/candles` 등 엔드포인트 **파라미터/응답 형태 발견** 프로브 (`"symbol=005930&interval=1d"`) | 없음(stdlib) |
| `fetch_krx_symbols.py` | **KRX 심볼 시드 생성**(KOSPI+KOSDAQ → `data/krx_symbols.json`). out-of-band(틱 중 호출 아님), 주기적 갱신 | 없음(stdlib) |
| `live_check.py` | **운영 클라이언트**(httpx + Pydantic 모델)로 라이브 읽기 전용 점검 | 앱 패키지 |
| `llm_live_check.py` | **LLM 엔진 라이브 검증** — Opus 4.8 판단 1콜 + web_search 조사 1콜(유료·소액, 주문 무관) | 앱 패키지 + Anthropic 키 |
| `calibration_report.py` | **confidence 캘리브레이션** — BUY 판단의 t+5/t+20 수익률 버킷 분석(단조성 판정) | 앱 패키지 + DB |
| `run_local.py` | **로컬 서버 런처** — `.env` → process env 로드 후 uvicorn 구동(상시 운용 진입점, [USAGE.md](../../USAGE.md)) | 앱 패키지 |
| `tick_dry_run.py` | **전 거래 파이프라인**을 실계좌로 1회 DRY_RUN 실행(`[워치리스트]` 또는 `--seed [N]`) | 앱 패키지 |

```powershell
python server/scripts/toss_smoke.py
python server/scripts/probe_candles.py "symbol=005930&interval=1d"
python server/scripts/fetch_krx_symbols.py                  # KRX 시드 갱신(공개 데이터, 자격증명 무관)
python server/scripts/live_check.py
server/.venv/Scripts/python server/scripts/llm_live_check.py   # LLM 실가동 검증(Anthropic 키 필요)
python server/scripts/tick_dry_run.py 005930,000660         # 명시 워치리스트
python server/scripts/tick_dry_run.py --seed 15             # KRX 시드 상위 15 후보로 DRY_RUN
```

`.env` 탐색 순서: 스크립트 폴더(`server/scripts/`) → `server/` → 현재 작업폴더.

## 스모크가 확인하는 함정 (인사이트 §2.2)

| # | 확인 내용 |
|---|---|
| 1 | 리소스 경로 prefix `/api/v1` (토큰만 `/oauth2/token`, prefix 없음) |
| 2 | 모든 응답 `{ "result": ... }` 래핑 → `unwrap` |
| 3 | 계좌 헤더 = `accountSeq`(정수). 계좌번호 아님 → 자동 판별로 실증 |
| 4 | 금액: 루트=통화버킷 `{krw,usd}`, item=평문+`item.currency`, `rate`는 분수(×100) |
| 5·7 | `/prices`·`/stocks` 는 `symbols` 필수, `buying-power` 는 `currency` 필수 |

설계 근거는 [`../../TECH-STACK.md`](../../TECH-STACK.md), 사실 출처는
[`../../TOSS-AI-TRADING-INSIGHTS.md`](../../TOSS-AI-TRADING-INSIGHTS.md).
