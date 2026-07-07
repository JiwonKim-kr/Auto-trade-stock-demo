# 환경설정 · 사용 가이드

로컬에서 **자동 페이퍼 트레이딩**(실주문 0)을 켜두고 성과를 측정하는 것까지의 전 과정.
개요/설계는 [README.md](README.md) · [TECH-STACK.md](TECH-STACK.md), 토스 API 사실은
[TOSS-AI-TRADING-INSIGHTS.md](TOSS-AI-TRADING-INSIGHTS.md) 참조.

> ⚠️ **토스 Open API 에는 샌드박스가 없다 — 모든 키는 실계좌다.** 이 시스템은 DRY_RUN 기본이라
> 실주문을 보내지 않지만(주문 전송 코드 자체가 아직 없음), 키 관리는 실자금 기준으로 하라.

---

## 1. 사전 준비물

| 항목 | 어디서 | 필수? |
|---|---|---|
| Python 3.12+ | python.org (3.14 검증됨) | 필수 |
| 토스증권 Open API 키 | 토스증권 **PC 웹** → 설정 → Open API → 키 발급 | 필수 |
| Anthropic API 키 | console.anthropic.com | 선택 — 없으면 LLM 대신 결정적 폴백(관찰용) |

⚠️ Anthropic: 판단 모델 `claude-fable-5` 는 **30일 데이터 보존이 필수**(ZDR 조직은 400 에러).

## 2. 설치

```powershell
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q          # 215 passed 확인 = 설치 정상
```

## 3. 자격증명 · 설정 (.env)

```powershell
Copy-Item server/scripts/.env.example server/scripts/.env
```

`server/scripts/.env` 를 열어 채운다. **이 파일은 .gitignore 로 커밋이 차단**되어 있다
(`git check-ignore server/scripts/.env` 로 확인 가능). 절대 공유 금지.

```bash
# ── 필수: 토스 자격증명 ──
TOSS_CLIENT_ID=tsck_live_...
TOSS_CLIENT_SECRET=tssk_live_...
# TOSS_ACCOUNT_SEQ=1                  # 생략 시 자동 판별

# ── 상시 페이퍼 운용 4종 세트(§5) ──
DATABASE_URL=sqlite+aiosqlite:///./trading.db
SYMBOL_SOURCE_PATH=data/krx_symbols.json
TICK_INTERVAL_SEC=300
ANTHROPIC_API_KEY=sk-ant-...          # 선택(없으면 결정적 폴백)
```

**서버는 process env 만 읽는다** — `.env` 자동 로드는 없고, 로컬은 **런처가 명시적으로 로드**한다
(`scripts/run_local.py`, §5). 파일 위치에 따라 몰래 설정이 바뀌는 사고 방지. 전체 환경변수 표는
[README.md](README.md#환경변수).

## 4. 첫 점검 (읽기 전용 — 주문 미전송)

순서대로 한 번씩. 전부 통과하면 환경이 완성된 것이다.

```powershell
python server/scripts/toss_smoke.py                              # ① 토스 연결·accountSeq 판별
server/.venv/Scripts/python server/scripts/live_check.py         # ② 운영 클라이언트 점검
server/.venv/Scripts/python server/scripts/llm_live_check.py     # ③ LLM 실가동(키 있을 때, 유료·소액)
server/.venv/Scripts/python server/scripts/tick_dry_run.py       # ④ 전 파이프라인 1회 DRY_RUN
```

## 5. 상시 운용 시작 (로컬 페이퍼 트레이딩)

```powershell
server/.venv/Scripts/python server/scripts/run_local.py          # 기본 포트 8000
```

런처가 하는 일: `scripts/.env` → process env 로드 → 작업폴더를 `server/` 로 고정
(`trading.db` 위치 일관) → uvicorn 구동. 이후는 자동이다:

- **장중(KST 평일 09:00–15:30)에만** `TICK_INTERVAL_SEC` 간격으로 틱이 돈다. 장외엔 대기(LLM 비용 0).
- 틱마다: KRX 2,655종목 **코호트 로테이션**(40개씩 순환) → 스크리너 → (조사→LLM 판단 또는 폴백)
  → 비용 게이트·레짐 필터 → **페이퍼 체결**(실주문 0) → DB 기록.
- 페이퍼 장부(초기 자본 `PAPER_SEED_KRW`, 기본 1천만)가 LLM 의 '보유'가 되어 매도까지 평가된다.
- 킬스위치·서킷브레이커·페이퍼 장부는 **재시작해도 DB 에서 복원**된다. 그냥 껐다 켜도 된다.

## 6. 모니터링 · 제어 (API)

모든 `/api/*` 는 헤더 `X-API-Key` 필요(기본 `dev-local-key` — `API_KEY` 로 변경).

```powershell
$H = @{ "X-API-Key" = "dev-local-key" }
Invoke-RestMethod http://127.0.0.1:8000/api/status -Headers $H
```

| 엔드포인트 | 용도 |
|---|---|
| `GET /api/status` | 모드·킬스위치·**서킷브레이커 현황**·장중 여부·persistence·가드레일 한도 |
| `GET /api/evaluation` | **페이퍼 성과**: 누적수익·Sharpe(연환산)+SE·MDD·벤치마크 대비·판정 |
| `GET /api/reconcile` | 실계좌 ↔ 기준선 수동 대조(기준선 미이동) |
| `GET /api/orders` | 이번 세션 주문 원장(전체 이력은 DB `orders`) |
| `GET /api/holdings` · `/api/buying-power` · `/api/prices?symbols=` | 토스 프록시 |
| `POST /api/kill-switch` `{"engaged":true|false}` | 전 주문 수동 차단/해제(재시작 생존) |
| `POST /internal/tick` | 틱 수동 1회(자동 루프와 중복 시 직렬화 — 스킵 응답) |

**`/api/evaluation` 읽는 법**: 완결 트레이드 **N<100 동안 "판단 보류"가 정상**이다(운/실력 구분
불가 — study 규율). 수 주간 곡선을 쌓은 뒤 Sharpe·벤치마크 대비를 본다. LIVE 전환의 게이트.

## 7. 자동 안전장치 — 언제 걸리고 어떻게 풀리나

| 장치 | 발동 | 해제 |
|---|---|---|
| 킬스위치 | 수동(`/api/kill-switch`) 또는 LIVE 리컨실 불일치 시 자동 | **수동만**(원인 확인 후) |
| 서킷브레이커(일일) | 일일 손실 ≤ −5% | 다음 거래일 자동 |
| 서킷브레이커(낙폭) | 고점대비 −15% | **−8% 까지 회복 시** 자동(히스테리시스) |
| 비용 게이트 | 기대이동폭 < 라운드트립×3.5 인 매수 | 해당 없음(주문별 판정) |
| 레짐 필터 | 시장 σ ≥1% 노출 ×0.5 · ≥2% 신규 중단 | σ 하락 시 자동 |
| LLM 비용가드 | 일일 판단 `DAILY_LLM_DECISION_CAP` 도달 → 폴백 강등 | 다음 날 자동 |

공통 원칙: **어떤 장치도 매도(청산)는 막지 않는다** — 포지션 줄일 길은 항상 열려 있다.

## 8. 데이터 관리

- **DB**: `server/trading.db` (SQLite, gitignore). 테이블: `ticks`·`decisions`(LLM 근거 전수)·
  `orders`·`audit_log`·`engine_state`·`position_snapshots`/`positions`(리컨실)·
  `paper_state`/`paper_positions`/`paper_equity`(페이퍼).
- **페이퍼 리셋**(실험 다시 시작): 서버 끄고 `trading.db` 삭제 — 다음 기동 때 seed 로 재초기화.
  (틱/주문 이력도 함께 사라지니 보존하려면 파일 백업 후 삭제.)
- **KRX 시드 갱신**(상장/상폐 반영, 월 1회 권장): `python server/scripts/fetch_krx_symbols.py`

## 9. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| `/api/holdings` 503 | 토스 자격증명 미로드 — 런처로 실행했는지, `.env` 값 확인 |
| 401 Unauthorized | `X-API-Key` 헤더 누락/불일치(`API_KEY` 설정값 확인) |
| 토스 429 | BASIC tier 레이트리밋 — 클라이언트가 자동 백오프 재시도. `UNIVERSE_MAX_SYMBOLS` 축소 고려 |
| Anthropic 400 (data retention) | Fable 5 는 30일 보존 필수 — ZDR 조직이면 사용 불가 |
| `engine`에 "폴백" | `ANTHROPIC_API_KEY` 미설정 또는 일일 LLM 상한 도달(비용가드 — 정상 동작) |
| 틱 응답 `skipped` | 이전 틱 진행 중(직렬화) — 정상. 다음 주기에 실행됨 |
| 평가가 계속 "판단 보류" | 완결 트레이드 N<100 — 정상. 곡선이 쌓일 시간이 필요 |

## 10. ⚠️ LIVE 전환에 관하여 (지금은 불가)

`TRADING_MODE=LIVE` + `I_UNDERSTAND_LIVE_REAL_MONEY=YES` 를 **process env** 로 줘야 LIVE 가
되지만(파일만으론 안 됨 — 의도된 마찰), **주문 전송 executor 가 아직 없어** LIVE 로 켜도 주문은
`FAILED(executor 미설정)` 로 기록만 된다. 실제 LIVE 는 로드맵 M3(executor·체결 조회) 구현과
**페이퍼 평가 게이트(N≥100·유의성) 통과 후** 소액 1주부터.
