# 기술 스택 설계 — 토스 AI 자동매매 (데스크톱 + 클라우드)

> **전제**: 본 문서는 [`TOSS-AI-TRADING-INSIGHTS.md`](TOSS-AI-TRADING-INSIGHTS.md)의 인계 사실을
> **확정 제약**으로 받아들이고 그 위에 스택을 새로 설계한다. 토스 API의 7대 함정, 안전 골격
> (DRY_RUN·가드레일·킬스위치), GCP 토폴로지, 하이브리드 AI 엔진은 인사이트 문서를 단일 출처로 따른다.
>
> **작성 시점**: 2026-06. 버전은 해당 시점 최신 안정판 기준이며, 실제 핀 고정은 프로젝트 init 시 확정한다.

---

## 0. 결정 요약 (Decisions)

| 영역 | 선택 | 비고 |
|---|---|---|
| 거래 서버(두뇌) | **Python 3.12+ / FastAPI** | 퀀트·LLM 생태계, async, Pydantic 검증 |
| 데스크톱(눈) | **Tauri 2 + React/TS** | 경량 셸, 얇은 클라이언트 |
| 데이터베이스 | **Cloud SQL (PostgreSQL 16)** | 주문/포지션/감사 관계형 |
| 클라우드 | **GCP** (Cloud Run·Artifact Registry·Secret Manager·Cloud Scheduler) | 인사이트 §6 그대로 |
| AI | **Claude `claude-opus-4-8`** | 하이브리드 2단계, tool-use 구조화 출력 |
| 리전 | **`asia-northeast3`(서울)** | 프로토타입과 동일, KRX/토스 근접 |
| IaC / CI | **Terraform + GitHub Actions** | 재현 가능한 인프라·배포 |

> **Python 선택의 유일한 약점(데스크톱과 타입 비공유)은 해소된다**: FastAPI가 내보내는 OpenAPI
> 스키마에서 `openapi-typescript`로 TS 타입을 생성해 데스크톱이 소비한다 → **Python↔TS 경계에서도
> end-to-end 타입 안전**. TS 백엔드의 모노레포 타입 공유 이점을 사실상 회수한다.

---

## 1. 시스템 아키텍처

```
┌─────────────────────────┐         HTTPS (API Key / IAP)        ┌───────────────────────────────────────┐
│   데스크톱 앱 (눈)        │  ───────────────────────────────▶   │      Cloud Run  (두뇌: API + 거래로직)   │
│   Tauri 2 + React/TS     │   현황 조회 · 킬스위치 · 모드전환     │   FastAPI (async)                         │
│   - 노드그래프/캔들 뷰    │  ◀───────────────────────────────   │   ├─ Toss 클라이언트 (함정 내재화)         │
│   - API키 OS키체인 보관   │         서버 API 응답(JSON)          │   ├─ 스크리너(pandas-ta) → 후보            │
└─────────────────────────┘                                       │   ├─ AI 엔진(Claude opus-4-8, tool-use)   │
                                                                   │   ├─ 주문층(모드게이트+하드가드레일)       │
   ┌──────────────────────┐     OIDC (장중 N분 cron)              │   └─ 킬스위치/리컨실/감사로깅              │
   │  Cloud Scheduler      │  ─────────────────────────────────▶  │   POST /internal/tick                     │
   └──────────────────────┘                                       └───────────────┬───────────────────────┘
                                                                                   │
                          ┌────────────────────────┬───────────────────────────┬──┴──────────────────────┐
                          ▼                         ▼                           ▼                          ▼
                 ┌─────────────────┐      ┌──────────────────┐        ┌──────────────────┐      ┌──────────────────┐
                 │ Secret Manager  │      │  Cloud SQL (PG)  │        │  Toss Open API   │      │  Anthropic API   │
                 │ 토스 creds·키    │      │ 주문/포지션/감사  │        │ openapi.toss…    │      │ claude-opus-4-8  │
                 └─────────────────┘      └──────────────────┘        └──────────────────┘      └──────────────────┘
                                                                       (+ 외부 심볼 소스: KRX 종목목록)
```

**토폴로지 원칙(불변)**: 토스 자격증명·Anthropic 키는 **서버에만**. 데스크톱은 토스/Claude를 직접 호출하지
않고 서버 API만 부른다. 공개 대시보드 없음 → 노출면 최소.

---

## 2. 백엔드 (두뇌) 스택 — Python / FastAPI

| 구분 | 선택 | 이유 |
|---|---|---|
| 런타임 | **Python 3.12+** | 최신 타입힌트·성능. (3.13도 가능) |
| 웹 프레임워크 | **FastAPI** | async, Pydantic v2 검증, OpenAPI 자동 생성(→ 데스크톱 타입) |
| ASGI 서버 | **Uvicorn** (Cloud Run 단일 프로세스, 동시성은 async) | 컨테이너 1프로세스 권장 |
| HTTP 클라이언트 | **httpx** (async, 타임아웃·재시도) | 토스 호출용. `tenacity`로 백오프 |
| 데이터 검증/설정 | **Pydantic v2 + pydantic-settings** | 토스 `{result}` 언래핑·DTO·env 검증 |
| ORM / 마이그레이션 | **SQLAlchemy 2.0 (async) + Alembic**, 드라이버 **asyncpg** | 관계형·트랜잭션·리컨실 쿼리 |
| 퀀트/지표 | **pandas + numpy + pandas-ta**(또는 `ta`) | 결정적 스크리너. TA-Lib는 C의존성↑라 순수파이썬 우선 |
| LLM | **anthropic** (Python SDK), `claude-opus-4-8` | tool-use 구조화 출력, 프롬프트 캐싱 |
| 의존성/빌드 | **uv** | 빠르고 재현성 높은 lock. (poetry 대안) |
| 린트/포맷/타입 | **ruff** (lint+format) + **mypy** | |
| 테스트 | **pytest + pytest-asyncio + respx**(httpx 목) | 토스 응답 픽스처로 매핑 회귀 방지 |
| 컨테이너 | **Docker** (python:3.12-slim 베이스) | Artifact Registry로 푸시 |

**동시성/틱 모델**: 거래 틱은 Cloud Scheduler가 `POST /internal/tick`을 호출 → 요청 내에서
`수집→스크리너→LLM→가드레일→주문`을 동기 수행. **중복 틱 방지**를 위해 PG **advisory lock**(또는
`tick_runs` 상태행)으로 직렬화. 틱은 짧게(수 초~수십 초) 끝나도록 후보 수를 제한.

---

## 3. 데스크톱 (눈) 스택 — Tauri 2

| 구분 | 선택 | 이유 |
|---|---|---|
| 셸 | **Tauri 2** (Rust 코어 + 시스템 웹뷰) | 경량 바이너리·저메모리·보안 기본값 |
| UI 프레임워크 | **React 18 + TypeScript + Vite** | Next.js 프로토타입 지식 연속, 생태계 |
| 스타일/컴포넌트 | **Tailwind CSS + shadcn/ui** | 빠른 구축, 일관 디자인 |
| 서버 상태 | **TanStack Query** | 폴링/캐시/리트라이(시세 1초 폴링과 궁합) |
| 로컬 UI 상태 | **Zustand** | 가볍고 단순 |
| 노드 그래프 | **@xyflow/react (React Flow)** | 허브-스포크(보유+AI후보 시각구분) 재현 |
| 캔들/차트 | **lightweight-charts** (TradingView) | 캔들/시세 시각화 |
| API 클라이언트 | **openapi-typescript + openapi-fetch** | FastAPI 스키마에서 타입 생성 → 타입드 호출 |
| 비밀 저장 | **OS 키체인** (`tauri-plugin-stronghold` 또는 keyring) | 서버 API 키 안전 보관(평문 금지) |
| 자동 업데이트 | **Tauri Updater** | 서명 배포 |
| 테스트 | **Vitest + Playwright** | 컴포넌트/E2E |

**색상·통화 규칙(인사이트 §5 준수)**: 수익 **빨강** / 손실 **파랑** / 보합 **회색**(한국 관습),
통화 라벨 필수(KRW "원", USD "300.56 USD") — 해외 종목을 "원"으로 찍지 않는다.

**데스크톱의 역할 한계**: 조회·제어(킬스위치, DRY_RUN↔LIVE 전환 요청, 한도 조정)만. 거래 판단/실행은
전부 서버. 데스크톱에는 토스/Anthropic 키가 **존재하지 않는다**.

---

## 4. 데이터 계층 — Cloud SQL (PostgreSQL 16)

**핵심 원칙**: 토스 금액은 **문자열 + 통화 버킷**으로 온다. 저장은 **`NUMERIC`(가격/수량) + 행마다
`currency` 컬럼**, 코드에선 **`Decimal`만 사용(float 금지)**. `profitLoss.rate`는 분수이므로 표시 시 ×100.

**스키마 스케치**(초안):

| 테이블 | 핵심 컬럼 | 목적 |
|---|---|---|
| `accounts` | account_no, **account_seq**, account_type | 헤더용 식별자(정수 seq!) |
| `universe_symbols` | symbol, market, source, is_common_share, leverage_factor, status… | 외부 심볼 소스 + 마스터 플래그(보수적 제외) |
| `positions` | symbol, currency, quantity(Decimal), avg_price, market_value, pl_rate | 보유 스냅샷 |
| `ticks` | started_at, status, candidates_count, notes | 틱 실행 직렬화/추적 |
| `decisions` | tick_id, symbol, action(BUY/SELL/HOLD), size, **rationale(text)**, model | LLM 근거 전수 로깅 |
| `orders` | **client_order_id(UNIQUE)**, symbol, side, type, qty, price, mode, status, toss_order_id | 멱등키·DRY/LIVE·상태 |
| `audit_log` | ts, actor, action, payload(jsonb), result | 전 결정·주문·모드전환 감사 |
| `guardrail_state` | kill_switch(bool), daily_buy_used, limits(jsonb) | 킬스위치·한도 현재값 |

- 마이그레이션: **Alembic**. 접속: Cloud Run → Cloud SQL은 **Cloud SQL Connector**(또는 유닉스 소켓),
  비밀번호/접속정보는 **Secret Manager**.
- `client_order_id` **UNIQUE 제약**으로 DB 레벨에서도 중복주문 차단(멱등 2중 방어).

---

## 5. AI 엔진 — 하이브리드 2단계

```
유니버스(외부 심볼 + 마스터 플래그 보수적 제외)
        │  ── 결정적 기술지표 스크리너 (pandas-ta)  →  소수 후보로 압축
        │      └─ [하드 가드레일 1차: LLM이 못 넘는 안전선 — 우선주/레버리지/정지/장외시간/한도]
        ▼
   후보(per-symbol enrich: warnings·저유동성·동전주)
        │  ── Claude claude-opus-4-8 (tool-use / JSON 구조화 출력)
        │      → BUY/SELL/HOLD + 사이징 + 근거 텍스트
        ▼
   결정 로깅(decisions) → [하드 가드레일 2차: 주문 직전 재검사] → 주문층
```

- **구조화 출력**: anthropic SDK의 tool-use(함수 스키마)로 `{action, symbol, size, confidence, rationale}`
  강제 → 파싱 견고. **시스템 프롬프트 캐싱**으로 비용 절감.
- **근거 전수 로깅**: 모든 결정의 rationale을 `decisions`에 저장(감사·사후분석).
- **가드레일은 LLM 바깥**: 진입 전(후보 압축)·주문 직전 2회 결정적 검사. LLM은 가드레일을 통과한
  공간에서만 판단. (순수 LLM 환각/순수 규칙 경직의 절충)
- **비용 인지 진입 게이트(결정→사이징 사이)**: 기대이동폭 ≥ 라운드트립 비용 × 3.5(기본) 인 매수만 통과,
  비용에 갉아먹히는 잔매매를 차단. ⚠️ LLM은 방향+confidence만 주고 **기대이동폭(magnitude)을 안 준다**
  → 결정적 **프록시** `confidence × 실현변동성 σ × move_multiple`로 추정(정밀 예측 아님, 엣지<비용 차단이 목적).
  라운드트립 = 2×수수료 + 2×슬리피지 + 매도세. 증권거래세는 2025~ **0.15%** 가정 — 세율/슬리피지는
  **실거래 시 재확인·보정**(슬리피지 종목별 정교화는 지능형 사전선별 이후).
- 구현 전 **`claude-api` 가이드 참조**, 최신 Claude 모델 사용.

---

## 6. 토스 API 통합 계층 (함정 내재화)

`server/app/toss/` 모듈로 인사이트 §2를 코드 불변식으로 박는다:

- **베이스/경로**: `https://openapi.tossinvest.com`, 리소스는 **`/api/v1`**, 토큰만 `/oauth2/token`(prefix 없음).
- **언래핑 헬퍼**: 모든 응답 `{result}` 자동 벗김(`unwrap`/`unwrap_list`).
- **계좌 헤더**: `X-Tossinvest-Account: <account_seq>`(정수). 계좌번호 넣지 않음.
- **통화 모델(Pydantic)**: holdings 루트는 `{krw, usd}` 중첩 버킷, item은 그 종목 통화 평문 + `item.currency`.
  `rate`는 분수. → 루트/아이템 **모델을 분리**해 매핑 어긋남 방지.
- **필수 파라미터 가드**: `/prices`·`/stocks`는 `symbols`, `buying-power`는 `currency` 없으면 호출 거부(사전 검증).
- **취소/정정은 POST**: `/orders/{id}/cancel`, `/orders/{id}/modify`.
- **토큰 캐시**: 인메모리 + 만료(~24h) 전 선갱신.
- **WebSocket 없음** → REST 폴링(최대 1초). 등락률/거래량은 `candles`/`trades`로 별도 취득.

**스모크 우선(인사이트 §4)**: `server/scripts/toss_smoke.py`를 **가장 먼저** 작성 — 토큰→accounts→
(account_seq 자동 판별)→holdings/buying-power→prices→stocks→warnings의 **원시 응답 덤프**, **주문 절대 미전송**.
실응답으로 Pydantic 모델 확정 후에야 본 코드 작성.

---

## 7. 주문 / 안전 계층 (실자금 — 양보 없음)

- **모드 게이트**: `TRADING_MODE = DRY_RUN(기본) | LIVE`. DRY_RUN은 실 `POST /orders` 미호출, "의도된 주문"만 기록.
  LIVE 전환은 **명시·다중 확인**(env + 데스크톱 확인 + 서버 측 2단계).
- **하드 가드레일(모드 무관 동일)**: 글로벌 **킬스위치**, 1주문 최대 금액, **일일 매수 한도**,
  **종목당 비중 상한(기본 10%)**, 최대 포지션 수(기본 10), **KRX 장시간 게이트(09:00–15:30 KST)**,
  휴장일(`market-calendar/KR`) 체크. 종목당 10%는 `max_positions=10`과 정합 — 10×10%로 완전 배포는
  가능하되 단일 종목 집중을 막는다(다각화). breadth(6%+종목수↑)는 지능형 사전선별 층 이후로 미룸.
- **손실 서킷브레이커(신규 진입 자동 차단)**: 일일 손실 **−5%** 또는 고점대비 낙폭(MDD) **−15%** 도달 시
  **신규 매수만 차단하고 청산(매도)은 계속 허용**. 낙폭은 **−8%까지 회복돼야 해제**(히스테리시스), 일일
  손실은 **다음 거래일 자동 리셋**. _근거_: 1주문/일일 한도 같은 **순간 노출 상한은 매도 후 재투입을 반복하면
  누적 손실을 못 막는다** → 낙폭 기반 별도 차단이 필요. 손실 국면엔 자동 디레버리징하되 포지션을 줄일
  길(청산)은 항상 열어둔다. 상태(고점·발동 래치)는 주문 서비스가 소유하고 가드레일은 주입된 상태만 읽는다.
  튜너블: `DAILY_LOSS_LIMIT`·`MAX_DRAWDOWN_LIMIT`·`DRAWDOWN_REARM`(기본 0.05·0.15·0.08).
- **멱등**: `clientOrderId`(앱 생성) + DB UNIQUE.
- **리컨실**: 주기적으로 DB 포지션 ↔ 토스 `holdings` 대조, 불일치 시 알림·거래 중단.
- **감사**: 모든 결정·주문·모드전환·킬스위치 조작을 `audit_log`에 전수 기록.
- **LIVE 첫 전환**: **소액 1주**부터 → 리컨실/감사로 검증하며 점진 확대.

---

## 8. 인증 / 보안 (데스크톱 ↔ 서버)

- **Cloud Run 접근**: 두 경로 분리.
  - **Scheduler → /internal/tick**: **OIDC 토큰**(전용 서비스 계정). 내부 전용.
  - **데스크톱 → 공개 API**: **API 키 헤더**(서버가 상수시간 검증, 키는 Secret Manager). 키는 데스크톱
    **OS 키체인**에 보관. 강화 옵션으로 **IAP**(Google 로그인) 승급 가능(인사이트 §6의 "API키 또는 IAP").
- **전송**: HTTPS 전용(Cloud Run TLS). 레이트 리밋, 요청 검증, 최소 권한 SA.
- **비밀**: 토스 creds·Anthropic 키·DB·API 키 전부 **Secret Manager**(env 하드코딩 금지).

---

## 9. GCP 인프라 / 배포

| 서비스 | 용도 | 설정 포인트 |
|---|---|---|
| **Cloud Run** | FastAPI(두뇌) | `min=0`(서버리스+스케줄러 틱), 인증 필요, 동시성 적정값 |
| **Artifact Registry** | Docker 이미지 | 리전 `asia-northeast3` |
| **Cloud Scheduler** | 장중 N분 틱 | OIDC로 `/internal/tick` 호출, KST cron |
| **Secret Manager** | 토스·Anthropic·DB·API 키 | 버전 관리, SA 접근 최소화 |
| **Cloud SQL (PostgreSQL 16)** | 거래 데이터 | 사설 IP/Connector, 자동 백업 |
| **IaC** | Terraform | 위 전부 코드화(재현·리뷰) |
| **CI/CD** | GitHub Actions | 빌드→AR 푸시→Cloud Run 배포, PR에 lint/test 게이트 |

**거래 루프**: KR 분 단위 전략이면 **서버리스+스케줄러로 충분**(인사이트 §6). 초단타로 갈 때만
Cloud Run `min≥1` 상시 구동으로 승급.

---

## 10. 레포 구조 (폴리글랏 모노레포)

```
Auto-trade-stock-demo/
├─ server/                  # Python FastAPI (두뇌)
│  ├─ app/
│  │  ├─ api/               # 라우트(공개 API + /internal/tick)
│  │  ├─ toss/              # 토스 클라이언트(함정 내재화) + Pydantic 모델
│  │  ├─ engine/            # 스크리너(pandas-ta) + AI(Claude)
│  │  ├─ orders/            # 주문층 + 하드 가드레일 + 킬스위치
│  │  ├─ db/                # SQLAlchemy 모델 + Alembic
│  │  └─ core/              # config(pydantic-settings)·보안·로깅
│  ├─ scripts/toss_smoke.py # ★ 가장 먼저 작성
│  ├─ tests/
│  ├─ pyproject.toml        # uv
│  └─ Dockerfile
├─ desktop/                 # Tauri 2 + React/TS (눈)
│  ├─ src/                  # React UI(노드그래프·캔들·제어판)
│  ├─ src-tauri/            # Rust 셸(키체인·업데이터)
│  └─ package.json
├─ shared/
│  └─ api-types/            # FastAPI OpenAPI → openapi-typescript 생성물
├─ infra/                   # Terraform(Cloud Run·SQL·Secret·Scheduler)
├─ .github/workflows/       # CI/CD
├─ Taskfile.yml             # 교차 언어 태스크 러너(또는 Makefile/justfile)
├─ TOSS-AI-TRADING-INSIGHTS.md
└─ TECH-STACK.md            # (이 문서)
```

- **교차 언어 태스크**: `Taskfile`로 `task smoke / dev / test / gen-types / deploy` 통일.
- **타입 생성 플로우**: 서버 OpenAPI export → `shared/api-types` 갱신 → 데스크톱이 import.

---

## 11. 킥오프 순서 (인사이트 §7에 스택 매핑)

1. 공식 **openapi.json / README** 확보(§2.0).
2. **`toss_smoke.py`** 작성·실행 — 원시 덤프 + `account_seq` 자동 판별, **주문 0 보장**.
3. 실응답으로 **Pydantic 모델 확정**(통화 버킷·rate 분수 주의).
4. **DRY_RUN 주문층 + 가드레일/킬스위치**(테스트로 실주문 0 검증)부터.
5. **유니버스(외부 심볼+마스터 제외) → 스크리너 → Claude 판단** 연결, 결정 로깅.
6. **Terraform 인프라**: Secret Manager → Cloud SQL → Cloud Run 배포 → Scheduler 틱.
7. **데스크톱 뷰어**(타입드 클라이언트로 현황·제어), OS 키체인 API키.
8. **LIVE 전환**: 소액 1주 → 리컨실·감사로 검증하며 점진 확대.

---

## 12. 핵심 위험 & 대응

| 위험 | 대응 |
|---|---|
| 실자금 오발주 | DRY_RUN 기본·다중확인 LIVE·하드 가드레일·멱등·소액 1주 시작 |
| 손실 누적·연속 하락 | **손실 서킷브레이커**: 일일 −5%·MDD −15% 시 신규 진입 자동 차단(청산 허용), −8% 회복 시 해제(히스테리시스) |
| 토스 매핑 어긋남 | 스모크 우선·Pydantic 모델 픽스처 회귀테스트·`{result}`/통화/seq 불변식 |
| 비용 폭주(LLM) | 2단계로 후보만 LLM·프롬프트 캐싱·일일 호출 상한 |
| 장외/휴장 주문 | KRX 장시간 게이트 + market-calendar 체크 |
| 비밀 노출 | Secret Manager·키체인·데스크톱에 creds 미보관·공개 대시보드 없음 |
| 중복 틱 경쟁 | PG advisory lock으로 틱 직렬화 |
| 콜드스타트 지연 | 분 전략엔 허용. 필요 시 `min≥1` 승급 |

---

## 부록. 권장 라이브러리 핀(초안, init 시 확정)

- **server**: python 3.12, fastapi, uvicorn, httpx, tenacity, pydantic v2, pydantic-settings,
  sqlalchemy 2.0, asyncpg, alembic, pandas, numpy, pandas-ta(또는 ta), anthropic, ruff, mypy,
  pytest, pytest-asyncio, respx, uv, cloud-sql-python-connector.
- **desktop**: tauri 2, react 18, typescript, vite, tailwindcss, shadcn/ui, @tanstack/react-query,
  zustand, @xyflow/react, lightweight-charts, openapi-typescript, openapi-fetch, vitest, playwright,
  tauri-plugin-store/stronghold.
- **infra**: terraform, google provider, docker, github actions.
