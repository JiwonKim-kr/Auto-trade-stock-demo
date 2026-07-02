# 토스 기반 AI 자동매매 (Toss AI Auto-Trading)

토스증권 Open API 기반의 AI 자동매매. **클라우드 서버(두뇌)** 가 장중 자율 거래를 돌리고
**데스크톱 앱(눈)** 은 현황 조회·제어만 한다. 실자금이므로 **안전 우선**(DRY_RUN 기본·하드 가드레일·킬스위치).

> **상태:** 거래 서버(두뇌)의 **거래 파이프라인 골격 완성** — 수집 → 유니버스 → 스크리너 → 조사 →
> LLM 판단 → 결정적 사이징 → DRY_RUN 주문. **KRX 심볼 소스**(KOSPI+KOSDAQ 시드) · **손실 서킷브레이커**
> · **비용 인지 진입 게이트** · **레짐 필터**(고변동 국면 노출 축소) · **DB 영속화**(엔진 상태 재시작 생존)
> · **리컨실**(포지션 대조 — LIVE 불일치 시 자동 거래 중단) · **페이퍼 P&L + 평가**(모의 체결 넷 손익 →
> Sharpe/MDD/표본 게이트). **206개 테스트 통과**, **실계좌 DRY_RUN end-to-end 검증(실주문 0)**.
> 데스크톱 앱·GCP 인프라·LIVE 전환은 [로드맵](#로드맵) 참조.

---

## 아키텍처

```
┌──────────────────────┐    HTTPS(API키/IAP)    ┌──────────────────────────────────────┐
│  데스크톱 앱 (눈)      │ ───────────────────▶  │   Cloud Run (두뇌: FastAPI + 거래로직) │
│  Tauri 2 + React/TS   │   현황·킬스위치·모드    │   ├─ 토스 클라이언트(함정 내재화)        │
│  (예정)               │ ◀───────────────────  │   ├─ 엔진: 유니버스→스크리너→조사→LLM   │
└──────────────────────┘       JSON             │   ├─ 주문층(모드게이트+하드가드레일)     │
                                                 │   └─ 킬스위치/리컨실/감사               │
   ┌──────────────────┐   OIDC(장중 N분 cron)    │   POST /internal/tick                  │
   │  Cloud Scheduler  │ ─────────────────────▶  └───────┬────────────────────────────────┘
   └──────────────────┘                                  │
              ┌────────────────┬───────────────────┬─────┴──────────┬──────────────────┐
              ▼                ▼                   ▼                ▼                  ▼
       Secret Manager     Cloud SQL(PG)      토스 Open API    Anthropic API     KRX 심볼 시드
       토스·AI 키         주문/포지션/감사    openapi.toss…    claude-fable-5     KOSPI+KOSDAQ
```

**토폴로지 원칙:** 토스/Anthropic 키는 **서버(Secret Manager)에만**. 데스크톱엔 서버 호출용 API 키만 둔다
(데스크톱은 토스/AI를 직접 호출하지 않음). 자율 거래가 데스크톱 on/off와 무관하게 돌도록 키는 서버 보관.

## 거래 파이프라인

```
KRX 시드(KOSPI+KOSDAQ) ∪ 워치리스트 ∪ 보유 종목   ← 후보 상한으로 캔들 호출 수 제어(레이트리밋)
  → stocks(마스터) enrich
  → 유니버스 보수적 제외 (우선주·레버리지·정리매매·거래정지·SPAC/ETN)
  → 스크리너 (이동평균·RSI·유동성·동전주 → 후보 압축)            ← 보유 종목은 매도 평가 위해 항상 포함
  → 조사 (Claude web_search 로 최신 뉴스/이벤트 grounded 브리프)   ← 비용 위해 상위 N만
  → 레짐 필터 (시장 프록시 σ: CALM×1 · ELEVATED×0.5 · STRESS×0)   ← 거시=예측 아님, 노출 축소로만 대응
  → LLM 판단 (claude-fable-5: BUY/SELL/HOLD + confidence)        ← 사이징은 안 함
  → 비용 인지 진입 게이트 (기대이동폭=confidence×σ×배수 ≥ 라운드트립×3.5 인 매수만)  ← 엣지<비용 잔매매 차단
  → 결정적 allocator (매수여력·1주문/일일 한도·종목당 비중 안에서 수량 × 레짐 배수)
  → 하드 가드레일 → DRY_RUN 주문 (실주문 0)
```

## 안전 모델

- **DRY_RUN 기본** — 실 `POST /orders` 미호출. LIVE는 `TRADING_MODE=LIVE` **와** `I_UNDERSTAND_LIVE_REAL_MONEY=YES` 다중 확인.
- **하드 가드레일은 LLM 바깥에서 강제** — 킬스위치 · 1주문/일일 매수 한도 · 종목당 비중 · 최대 포지션 수 · KRX 장시간(09:00–15:30 KST).
- **손실 서킷브레이커** — 일일 손실(−5%) 또는 고점대비 낙폭(MDD −15%) 도달 시 **신규 매수 자동 차단**, 청산(매도)은 허용. 낙폭은 −8%까지 회복돼야 해제(히스테리시스), 일일 손실은 다음 거래일 리셋.
- **레짐 필터** — 거시·지정학은 **예측하지 않고 대응**: 시장 프록시(KODEX 200) 일간 σ가 높은 국면엔 신규 매수 노출을 결정적으로 축소(×0.5)·중단(×0). 청산 경로는 축소하지 않음. LLM 프롬프트에도 "거시는 예측 시그널 아님" 제약 인코딩.
- **LLM은 사이징하지 않음** — 방향+confidence만, 수량은 결정적 코드. 미보유 SELL → HOLD 강등.
- **멱등** — `clientOrderId` 인메모리 1차 + **DB UNIQUE 2차 방어**(영속화 설정 시).
- **재시작 생존** — 킬스위치·서킷브레이커 래치·**일일 매수 사용액**이 DB로 생존(`DATABASE_URL` 설정 시). 일일 한도는 틱 경계 너머로 강제(이전엔 틱 내부에서만 누적).
- **리컨실** — 매 틱 **틱 전에** 시스템 기대 포지션(직전 스냅샷+전송 주문 순증감) ↔ 토스 실보유 대조. 불일치는 감사 기록, **LIVE 면 킬스위치 자동 발동**(DRY_RUN 은 기록만 — 수동 매매가 정상인 관찰 단계). 수동 점검 `GET /api/reconcile`. ⚠️ 전송(SUBMITTED) 기준 근사라 미체결/부분체결도 불일치로 뜸(보수적 오탐 — 체결 API 연동 시 정밀화).
- **키는 서버 보관** — 데스크톱 분실해도 브로커/AI 키 안전.

## 페이퍼 P&L · 평가 (DRY_RUN → LIVE 게이트)

DRY_RUN 은 실체결이 없어 손익이 없다 → **페이퍼 장부가 파이프라인을 구동**하는 자기일관 루프로
"이 시스템이 실제로 돌았다면?" 을 측정한다(`PAPER_SEED_KRW`, 기본 1천만 — DB 필요, 0=비활성):

- LLM 이 **페이퍼 보유**를 매도 평가하고, 사이징이 **페이퍼 현금**을 쓴다(진입→청산 완결 루프).
- 모의 체결 = 지정가 + 슬리피지 불리 방향 + 수수료/매도세 차감 — **모든 손익은 넷(net)**.
- 매 틱 자산곡선 기록(벤치마크=시장 프록시 동시 기록) → `GET /api/evaluation`:
  누적수익 · Sharpe(연환산)+표준오차 · MDD · 벤치마크 대비 · **완결 트레이드 N<100 이면 "판단 보류"**
  (표본 부족 시 운/실력 구분 불가 — LIVE 전환 게이트).
- ⚠️ 한계(정직하게): 즉시 전량 체결 가정(미체결 모형 없음) · SE는 iid 가정 · 벤치마크 대비는 베타 미조정.

## 레포 구조

```
.
├─ README.md                     (이 문서)
├─ TECH-STACK.md                 기술 스택 설계
├─ TOSS-AI-TRADING-INSIGHTS.md   토스 Open API 실전 인계 문서(사실 출처)
└─ server/                       거래 서버(두뇌) — Python / FastAPI
   ├─ app/
   │  ├─ toss/      토스 클라이언트(함정 내재화) + Pydantic 모델
   │  ├─ orders/    주문층(모드게이트·가드레일·킬스위치·서킷브레이커·리컨실) + holdings→context
   │  ├─ engine/    symbols · universe · screener · indicators · research · llm · costs · regime · allocator · pipeline · paper · evaluation
   │  ├─ db/        영속화(SQLAlchemy async) — 틱/결정/주문/감사 + 엔진 상태
   │  ├─ api/       FastAPI 라우트 + 인증
   │  └─ core/      설정 · 거래모드(다중확인)
   ├─ data/         KRX 심볼 시드 (krx_symbols.json — 페처가 갱신)
   ├─ scripts/      진단/점검 스크립트 (.env 는 여기, gitignore)
   ├─ tests/        206개 테스트 (+ 실응답 픽스처)
   └─ pyproject.toml
```

## 빠른 시작 (로컬)

사전: Python 3.12+ · 토스 Open API 키(토스증권 PC 웹 → 설정 → Open API). (LLM/조사 실가동엔 Anthropic 키.)

```bash
# 1) 의존성
cd server
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows (Linux/macOS: .venv/bin/python)

# 2) 토스 자격증명
cp scripts/.env.example scripts/.env
#   → scripts/.env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 채우기 (커밋 차단됨)

# 3) 진단·점검 (읽기 전용, 주문 미전송)
.venv/Scripts/python scripts/toss_smoke.py          # 원시 응답 덤프 + accountSeq 자동 판별
.venv/Scripts/python scripts/live_check.py          # 운영 클라이언트 라이브 점검
.venv/Scripts/python scripts/tick_dry_run.py 005930,000660   # 전 파이프라인 DRY_RUN

# 4) 테스트
.venv/Scripts/python -m pytest -q                   # 206 passed

# 5) 서버 (DRY_RUN, 토스 엔드포인트는 creds 필요)
.venv/Scripts/python -m uvicorn app.main:app --reload
```

`/internal/tick` 가 전 파이프라인을 돈다(Anthropic 키 있으면 Fable 5 + web_search, 없으면 결정적 폴백 판단기).

## 환경변수

| 변수 | 용도 | 기본 |
|---|---|---|
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` | 토스 Open API 자격증명 | (필수) |
| `TOSS_ACCOUNT_SEQ` | 계좌 시퀀스(미설정 시 자동 판별) | 자동 |
| `API_KEY` | 데스크톱 ↔ 서버 인증 키 | `dev-local-key`(운영 전 변경) |
| `ANTHROPIC_API_KEY` | AI 엔진(없으면 결정적 폴백) | 없음 |
| `DATABASE_URL` | DB 영속화(미설정 시 인메모리 — 운영 필수). 예: `postgresql+asyncpg://…` · `sqlite+aiosqlite:///./trading.db` | 없음 |
| `PAPER_SEED_KRW` | 페이퍼 P&L 초기 자본(DRY_RUN+DB 시 활성, 0=비활성) | 10000000 |
| `WATCHLIST` | 워치리스트(쉼표 구분, 심볼 소스보다 우선) | 빈 값 |
| `SYMBOL_SOURCE_PATH` | KRX 심볼 시드 경로(미설정 시 워치리스트만) | 없음 |
| `UNIVERSE_MAX_SYMBOLS` | 한 틱 후보 상한(캔들 레이트리밋 보호) | 40 |
| `TRADING_MODE` | `DRY_RUN`(기본) / `LIVE` | `DRY_RUN` |
| `I_UNDERSTAND_LIVE_REAL_MONEY` | LIVE 2차 확인(`YES`) | 없음 |
| `PER_ORDER_MAX_KRW` · `DAILY_BUY_CAP_KRW` · `MAX_POSITIONS` · `PER_SYMBOL_MAX_WEIGHT` | 가드레일 한도 | 100000 · 500000 · 10 · 0.10 |
| `DAILY_LOSS_LIMIT` · `MAX_DRAWDOWN_LIMIT` · `DRAWDOWN_REARM` | 서킷브레이커(일일손실·MDD·해제) | 0.05 · 0.15 · 0.08 |
| `ENTRY_COST_MULTIPLE` · `ENTRY_MOVE_MULTIPLE` | 진입 게이트(문턱 배수 · 기대이동 배수) | 3.5 · 3.0 |
| `REGIME_SYMBOL` (빈 값=비활성) · `REGIME_CALM_VOL` · `REGIME_STRESS_VOL` | 레짐 필터(시장 프록시·σ 임계) | 069500 · 0.010 · 0.020 |
| `COST_COMMISSION_RATE` · `COST_SLIPPAGE_RATE` · `COST_SELL_TAX_RATE` | 비용 모델(편도 수수료·슬리피지·매도세) | 0.00015 · 0.0015 · 0.0015 |

## 주요 결정 (ADR 요약)

- **스택:** Python/FastAPI(두뇌) · Tauri 2 + React(데스크톱) · Cloud SQL(PostgreSQL) · GCP.
- **키는 서버(Secret Manager):** 데스크톱이 꺼져 있어도 **클라우드 자율 거래**가 돌아야 하므로. "클라이언트 전용 보관"은 자율성을 잃거나(팻 클라이언트) 더 위험(서버 전달)해 채택 안 함.
- **AI:** `claude-fable-5`(최상위) + `claude-opus-4-8` 서버사이드 폴백. 조사는 `web_search`. ⚠️ Fable 5는 **30일 데이터 보존 필수**(ZDR 조직은 400).
- **사이징은 결정적 코드:** LLM은 방향+confidence만(실자금에서 LLM 숫자 신뢰 최소화). **매도(청산) 경로** 포함.
- **비용 인지 진입 게이트:** 기대이동폭 ≥ 라운드트립 비용 × 3.5 인 매수만(비용에 갉아먹히는 잔매매 차단). LLM이 magnitude를 안 주므로 기대이동폭은 **결정적 프록시**(confidence × 실현변동성 σ × 배수)로 추정. ⚠️ 증권거래세 0.15%(2025~) 가정 — 실거래 시 재확인.
- **방법론:** 공식 스펙 → 스모크로 실응답 확인 → 매핑/코드(추측 금지). 실응답을 픽스처로 회귀 고정.

## 로드맵

1. ✅ **KRX 외부 심볼 소스** — KOSPI+KOSDAQ 시드(2,655종목) + `SymbolSource` 추상화. **다음:** 시총/유동성 기반 지능형 사전선별(top-N) — 현재는 단순 후보 상한.
2. ✅ **DB 영속화** — 틱/결정/주문/감사 + 엔진 상태(킬스위치·서킷브레이커·일일 사용액) 재시작 생존. SQLAlchemy async(로컬 SQLite/운영 PG). **다음:** Cloud SQL 연결·Alembic(스키마 진화 시)·틱 직렬화(PG advisory lock).
3. **GCP 인프라(Terraform)** — Secret Manager · Cloud SQL · Cloud Run · Scheduler→`/internal/tick`(OIDC).
4. **데스크톱 앱(Tauri/React)** — 현황·킬스위치·모드.
5. **Anthropic 키 연동** — Fable 5 + web_search 실가동.
6. **LIVE 전환** — 리컨실·감사 검증 + **페이퍼 평가 게이트**(완결 트레이드 N≥100·유의성) 통과 후 **소액 1주**부터.

## 문서

- [TECH-STACK.md](TECH-STACK.md) — 기술 스택 설계(상세)
- [TOSS-AI-TRADING-INSIGHTS.md](TOSS-AI-TRADING-INSIGHTS.md) — 토스 Open API 실전 사실/함정(사실 출처)
- [server/scripts/README.md](server/scripts/README.md) — 진단/점검 스크립트
