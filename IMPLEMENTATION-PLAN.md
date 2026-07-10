# 구현·개선 계획서 (에이전트 핸드오프용)

> **이 문서의 목적**: 이후 작업을 이어받는 에이전트(모델 급과 무관)가 대화 맥락 없이 같은 품질로
> 구현하도록, **복잡한 로직은 알고리즘·수식·함정·통합 지점·테스트까지** 적는다.
> 현재 상태: M1 완료(로컬 상시 페이퍼 운용 가능, 215 테스트). 사용법은 [USAGE.md](USAGE.md),
> 설계 원칙은 [TECH-STACK.md](TECH-STACK.md), 토스 API 사실은 [TOSS-AI-TRADING-INSIGHTS.md](TOSS-AI-TRADING-INSIGHTS.md).
> 우선순위: **P0(안전 필수) → P1(성능/비용) → P2(클라우드 M2) → P3(LIVE M3) → P4(정교화)**.

---

## §0. 불변 원칙 — 구현 시 절대 위반 금지

1. **LLM 은 방향+confidence 만.** 수량·비중·기대이동폭 등 숫자는 결정적 코드가 계산한다.
2. **하드 안전장치는 LLM 바깥.** 가드레일·서킷브레이커·게이트는 LLM 이 우회 불가한 코드 경로에.
3. **어떤 자동 장치도 매도(청산)는 막지 않는다.** 유일한 예외 = 킬스위치(수동 완전 정지 — 의도됨).
4. **DRY_RUN 에서 executor 호출 0.** 이 불변식을 깨는 diff 는 어떤 이유로도 거부.
5. **돈은 Decimal, float 금지.** DB 저장은 정확 10진 문자열(SQLite 교차 정밀성). 통계량만 float 허용.
6. **DB/토스 I/O 는 경계(api/tick.py·routes·lifespan)에만.** `run_tick` 은 순수 오케스트레이션 —
   새 기능은 주입 파라미터(예: `entry_gate`, `regime_config`)로 넣는다.
7. **스모크 우선.** 외부 API 는 실응답 확인 전 매핑 코드를 확정하지 않는다. 실응답은 픽스처로 회귀 고정.
8. 기본값은 무회귀(opt-in) 또는 보수적. 새 안전장치는 fail-closed, 시장 전체 오버레이는 fail-open(×1)
   — 근거는 TECH-STACK §5 레짐 필터 항목.

---

## §1. P0 — 안전 필수 (LIVE 이전에 반드시)

### 1.1 ✅ LIVE 모드는 DB 필수 — 강제 강등 (구현됨)

**문제(점검 발견)**: 일일 매수 한도의 교차-틱 누적, 킬스위치/서킷브레이커 재시작 생존, 리컨실,
멱등 2차 방어가 **전부 DB 전제**다. 그런데 `DATABASE_URL` 없이 `TRADING_MODE=LIVE` 를 켤 수 있다
→ 이 상태에선 일일 한도가 **틱마다 리셋**되고(무한 매수 가능), 리컨실도 없다.

**구현** ([server/app/main.py](server/app/main.py) lifespan):
- 위치: `mode, warnings = load_trading_mode()` 직후 ~ `OrderService` 생성 사이에서 판정하되,
  repo 초기화가 mode 판정보다 뒤에 있으므로 **repo 초기화 후 mode 를 재검증**하는 블록을 넣는다:
  ```python
  if mode is TradingMode.LIVE and app.state.repo is None:
      mode = TradingMode.DRY_RUN
      app.state.order_service.mode = mode      # 이미 생성된 서비스도 강등
      logger.critical("LIVE 요청됐으나 DATABASE_URL 미설정 — DRY_RUN 강등 "
                      "(일일한도 누적·리컨실·멱등 2차방어가 DB 전제)")
  ```
  주의: `OrderService.mode` 는 평범한 속성이라 대입으로 강등 가능. 강등 후 `app.state.trading_mode` 도 갱신.
- **테스트**: env `TRADING_MODE=LIVE, I_UNDERSTAND_LIVE_REAL_MONEY=YES`(process env 로 주입) +
  `DATABASE_URL` 없음 → lifespan 후 `app.state.order_service.mode is DRY_RUN`.
  기존 `load_trading_mode` 테스트와 겹치지 않게 lifespan 통합 테스트로.

### 1.2 ✅ 결정적 청산 규칙 — 손절 · 타임스톱 (구현됨 — 페이퍼 대상. LIVE 확장은 §4.3 fills 후)

**문제(점검 발견)**: 진입은 다층 방어(스크리너→게이트→레짐→가드레일)인데 **청산은 LLM 재량뿐**이다.
개별 포지션이 −50% 가도 LLM 이 SELL 을 안 내면 방치된다(종목당 10% 상한 덕에 포트폴리오 피해는
−5%p 로 유계지만, 10종목 동반 하락이면 서킷브레이커 −15% 까지 무방비). study.md 의 원설계도
"청산 규칙(손절/타임스톱/시그널 소멸)이 보유기간을 결정"이었다. **안전과 효용(회전율→표본 축적)
양쪽의 핵심.**

**설계 — 경계 판정 + 강제 매도 주입** (pipeline 순수성 유지):

1. **데이터**: 포지션 진입 시각 필요.
   - [server/app/engine/paper.py](server/app/engine/paper.py) `PaperPosition` 에 `opened_at: datetime | None = None` 추가.
     `_fill_buy` 에서 신규 포지션 생성 시 `opened_at=now`(파라미터로 전달 — `apply_fill(req, cost, now)`
     시그니처 변경, 호출부는 tick.py 한 곳). **추가매수 시 opened_at 유지**(최초 진입 기준 — 타임스톱은
     "그 아이디어에 돈이 묶인 시간"을 재는 것).
   - DB [server/app/db/models.py](server/app/db/models.py) `PaperPositionRow` 에 `opened_at`(DateTime tz, nullable) 추가
     + repo save/load 왕복. 기존 행 호환: nullable 이므로 None 이면 타임스톱 미적용(다음 매수부터 적용).
   - LIVE 는 §4.3 fills 기반(선행 의존) — 이 단계는 페이퍼만으로 완결.

2. **판정 모듈** `server/app/engine/exits.py` (신규, 순수):
   ```python
   class ExitConfig(BaseModel):
       stop_loss_rate: Decimal = Decimal("0.08")   # 취득단가 대비 -8% 이하 → 강제 청산
       time_stop_days: int = 20                     # 보유 20 거래일 초과 → 강제 청산
       enabled: bool = True

   @dataclass
   class ForcedExit:
       symbol: str
       reason: str          # 예: "손절 −9.1% ≤ −8.0%" / "타임스톱 21거래일 > 20"

   def evaluate_exits(positions, marks, trading_days_held: dict[str, int],
                      cfg: ExitConfig) -> list[ForcedExit]: ...
   ```
   - 손절 판정: `(mark − avg_cost)/avg_cost ≤ −stop_loss_rate`. mark 없으면(시세 실패) **판정 보류**
     (허위 청산 방지 — 다음 틱에 재시도).
   - **거래일 수 계산**: 달력일이 아니라 거래일. 데이터 소스 = `paper_equity` 의 `trade_date` 목록
     (틱이 돌았던 날 = 거래일 근사). `trading_days_held[sym] = count(distinct trade_date >
     opened_at의 KST 날짜)`. repo 헬퍼 `count_trading_days_since(date) -> int` 추가
     (`SELECT COUNT(DISTINCT trade_date) FROM paper_equity WHERE trade_date > :d`).
   - 우선순위: 손절이 타임스톱보다 우선(사유 문자열에 반영).

3. **파이프라인 통합** ([server/app/engine/pipeline.py](server/app/engine/pipeline.py)):
   - `run_tick(..., forced_exits: list[ForcedExit] | None = None)` 파라미터 추가.
   - 8) 판단 단계에서: forced 심볼은 **LLM 판단을 건너뛰고**
     `Decision(action=SELL, symbol=..., confidence=1.0, rationale=f"결정적 청산: {reason}")` 생성.
     LLM 후보 리스트에서 제외(비용 절약 + LLM 이 HOLD 로 뒤집는 것 원천 차단 — §0-2).
   - 강제 SELL 은 allocator 에서 기존 SELL 경로(전량 청산) 그대로. 레짐 배수·비용 게이트 무관(§0-3).
4. **경계 조립** ([server/app/api/tick.py](server/app/api/tick.py)): 페이퍼 로드·마킹 직후
   `forced = evaluate_exits(paper.positions, marks, days_held, exit_cfg)` → `run_tick(forced_exits=forced)`.
   응답에 `"forced_exits": [...]` 노출 + decisions 기록엔 자동 포함(rationale 로 식별).
5. **설정**: `EXIT_STOP_LOSS_RATE=0.08`, `EXIT_TIME_STOP_DAYS=20`, `EXIT_RULES_ENABLED=true`.
6. **테스트**: (a) 손절 경계(정확히 −8% = 발동), (b) mark 없음 → 보류, (c) 타임스톱 거래일 카운트
   (같은 날 여러 틱 = 1거래일), (d) 강제 SELL 이 LLM 을 우회하고 주문 생성, (e) 킬스위치 시 REJECTED
   (킬스위치는 매도도 막음 — 의도), (f) opened_at None 하위호환.

### 1.3 ✅ 입출금 ↔ 서킷브레이커 왜곡 (경량 구현됨 — reset 엔드포인트. TWR 정석은 P4)

**문제(점검 발견)**: 서킷브레이커 HWM/낙폭은 자기자본 절대값 기준. LIVE 에서 **예수금 입금 → HWM
상향 → 직후 출금 → 낙폭 오탐**(반대로 입금이 실제 손실을 가릴 수도). 페이퍼는 폐쇄계라 무관.

**구현(경량)**: `POST /api/circuit-breaker/reset` 엔드포인트 — HWM 을 현재 자기자본으로 재설정
(+감사 기록 `actor=api, action=cb_reset`). 운영 절차 문서화: "입출금 후 반드시 reset 호출".
정석(자금 흐름 조정 수익률, TWR)은 P4 — 입출금 이벤트 테이블 + 시간가중 수익률로 equity 곡선 정규화.

---

## §2. P1 — 성능/비용 (페이퍼 데이터가 쌓이기 전에)

### 2.1 ✅ 캔들 캐시 — 토스 API 콜 92% 절감 (구현됨)

**문제(점검 발견)**: 캔들은 종목별 호출. 유니버스 40 × 78틱/일 ≈ **3,120콜/일**로 BASIC tier 429 의
주범이 될 구조. 그런데 일봉 데이터는 장중에 마지막(진행 중) 봉만 바뀐다 — 매 틱 재조회는 낭비.

**설계 — 주입형 캐싱 래퍼**(pipeline 무변경, §0-6):
```python
# server/app/toss/caching.py (신규)
class CachingToss:
    """toss 클라이언트 덕타이핑 래퍼 — get_candles 만 TTL 캐시, 나머지는 위임."""
    def __init__(self, inner, repo, ttl_minutes: int = 60): ...
    async def get_candles(self, symbol, interval="1d"):
        cached = await self._repo.get_cached_candles(symbol, interval)   # (fetched_at, json)
        if cached and now - fetched_at < ttl: return parse(cached)
        candles = await self._inner.get_candles(symbol, interval)
        await self._repo.save_cached_candles(symbol, interval, candles)  # upsert
        return candles
    def __getattr__(self, name): return getattr(self._inner, name)      # 위임
```
- DB: `candle_cache(symbol TEXT, interval TEXT, payload_json TEXT, fetched_at DateTime, UNIQUE(symbol, interval))`.
  payload 는 `[c.model_dump(mode="json") for c in candles]` — 역직렬화는 `Candle.model_validate`.
- 조립: tick.py 에서 `toss_for_tick = CachingToss(toss, repo) if repo else toss` 후 run_tick 에 전달.
  **리컨실·페이퍼 마킹의 holdings/prices 는 캐시하지 않는다**(실시간성 필요 — get_candles 만).
- TTL 트레이드오프 명시: 60분 캐시 = 스크리너 신호가 최대 60분 지연. 일봉 SMA/RSI 신호는 하루
  단위라 영향 미미. 진입가는 어차피 `last_close` 지정가 + 다음 틱 페이퍼 체결이라 일관.
- 효과: 40콜/틱 → 첫 틱 40콜 + 이후 시간당 40콜 ≈ 240콜/일 (**92% 절감**).
- **테스트**: TTL 내 재호출 시 inner 미호출(콜 카운터), TTL 경과 후 재조회, `__getattr__` 위임,
  repo 없으면 래핑 생략.

### 2.2 ✅ ADV(거래대금) 기반 지능형 사전선별 — 탐색/활용 2단계 (구현됨)

**문제**: 현행 코호트 로테이션은 공평하지만 무차별 — 유동성 없는 종목에 판단 예산을 똑같이 쓴다.
**설계 핵심**: 별도 데이터 소스 없이, **틱이 이미 받아온 캔들에서 ADV 를 공짜로 축적**한다.

1. **축적**: pipeline 4) 캔들 루프에서 계산한 값을 TickResult 에 실어 경계에서 저장하거나,
   간단히 tick.py 가 스크리닝 후 `repo.upsert_symbol_stats(symbol, adv20, last_trade_date)` 호출.
   `adv20 = mean(close_i × volume_i, 최근 20봉)` (Decimal). DB:
   `symbol_stats(symbol UNIQUE, adv20_krw TEXT, updated_trade_date TEXT)`.
2. **선정 알고리즘** (tick.py 유니버스 결정부 교체):
   ```
   pool_top   = symbol_stats 에서 adv20 상위 ADV_POOL_SIZE(기본 300)
   stale      = 시드 전체 중 미측정 or updated_trade_date 가 10거래일 이전
   n_explore  = ceil(limit × EXPLORE_RATIO(기본 0.2))          # 탐색 슬롯
   n_exploit  = limit − n_explore
   코호트     = rotate(pool_top, 틱수 × n_exploit)[:n_exploit]  # 상위 풀 내 로테이션
              + rotate(stale,   틱수 × n_explore)[:n_explore]   # 미측정 탐색
   ```
   - **콜드스타트**: symbol_stats 비어 있음 → pool_top 공집합 → 전 슬롯이 탐색 = **현행 로테이션과
     동일 동작으로 자연 시작**, 한 바퀴(≈66틱) 후 자동으로 활용 모드 전환. 마이그레이션 불필요.
   - 워치리스트 우선 포함은 기존과 동일(resolve_symbols include).
3. **테스트**: 콜드스타트=현행과 동일, 상위 풀 우선 선정, 탐색 슬롯이 stale 을 소진, 비율 경계(ceil).

### 2.3 ✅ 판단 결과 추적 — LLM confidence 캘리브레이션 (구현됨 — scripts/calibration_report.py)

**문제(점검 발견)**: 사이징이 `ceiling × confidence` 로 **LLM confidence 를 단조 신뢰**하는데,
그 confidence 가 실제 승률과 상관 있는지 측정 장치가 없다(전략 개선의 최우선 데이터).

**구현**:
1. `DecisionRow` 에 `decision_price TEXT nullable` 추가 — 판단 시점 last_close.
   pipeline 의 Decision 은 가격을 안 가지므로, record_tick 에서 `ctx_by[d.symbol].indicators.last_close`
   를 함께 저장하도록 `TickResult.decisions` 대신 (decision, price) 튜플…은 침습적 —
   **간단한 방법**: `Decision` 모델에 `decision_price: float | None = None` 필드 추가(extra 필드,
   스키마 요구 아님 — LLM 출력엔 없고 `_normalize` 후 pipeline 이 채움).
2. 분석 스크립트 `server/scripts/calibration_report.py`:
   - decisions × candle_cache(또는 캔들 재조회)로 각 판단의 **t+5·t+20 거래일 수익률** 계산.
   - confidence 버킷(0.5~0.6, …, 0.9~1.0)별: BUY 판단의 평균 수익률·승률·표본수 표 출력.
   - 해석 기준을 스크립트 출력에 포함: "버킷이 단조 증가하지 않으면 confidence 는 사이징 입력으로
     부적합 → allocator 를 계단 함수(예: conf<0.6 스킵, 0.6~0.8 half, >0.8 full)로 교체 검토".
3. **테스트**: 버킷 집계 순수 함수 분리 후 단위 테스트.

### 2.4 페이퍼 미체결 모형 — 지정가 대기 주문

**문제**: 현행 페이퍼는 "지정가 즉시 전량 체결" — 급등 추격 매수도 항상 성사되는 낙관 편향.
**설계**: 주문을 **대기 큐**에 넣고 다음 틱에서 실제 가격 경로로 체결 판정.
1. DB `paper_pending(id, symbol, side, quantity TEXT, limit_price TEXT, created_at, expires_trade_date TEXT)`
   — DAY 주문이므로 `expires = 주문일(KST)`.
2. 틱 시작부(페이퍼 로드 직후): pending 을 심볼별로 그날 캔들과 대조 —
   - 매수 체결 조건: 이후 관측된 `low ≤ limit_price` (판정용 low 는 당일 진행 봉 — CachingToss 캐시로
     최대 60분 지연 허용). 체결가 = `min(limit, open)` 근사 대신 **limit 그대로**(보수: 유리한 갭은 무시).
   - 매도 체결 조건: `high ≥ limit_price`.
   - 만료: `trade_date > expires` → 미체결 소멸(기록: fills 에 `skipped="만료"`).
3. 신규 주문은 즉시 체결하지 않고 pending 에 적재. **주의**: 같은 틱의 자산곡선은 현금이 아직
   안 나간 상태 — 대기 주문 명목액을 `reserved_cash` 로 표기해 이중 사용 방지(사이징 현금 =
   `cash − Σ pending buy notional`).
4. 트레이드오프 문서화: 체결 확실성은 현실화되지만 판정 지연(최대 1틱+캐시 TTL). 페이퍼 평가
   목적엔 보수 방향. `PAPER_FILL_MODEL=immediate|pending`(기본 immediate — 곡선 연속성 보존,
   전환은 명시적으로).
5. **테스트**: low 터치 체결/미터치 만료/reserved_cash 차감/모드 스위치 무회귀.

### 2.5 레짐 σ 를 EWMA 로 (반응성 개선)

현행 단순 표준편차(20일 균등가중)는 급변 반영이 느리다. RiskMetrics EWMA 로 교체:
```
σ²_t = λ·σ²_{t−1} + (1−λ)·r²_t ,  λ = 0.94 (일간 표준),  σ = √σ²_t
초기화: 첫 σ² = 첫 min_returns 개 수익률의 단순분산
```
[server/app/engine/regime.py](server/app/engine/regime.py) 에 `ewma_daily_vol(closes, lam=0.94)` 추가,
`RegimeConfig.vol_method: "ewma"|"simple"` (기본 ewma 전환 시 임계 재보정: EWMA 는 급변 시 simple 보다
크게 나오므로 calm/stress 1.0%/2.0% 유지하되 페이퍼 로그로 레짐 분포 확인 후 조정).
**테스트**: 균일 수익률에서 simple≈ewma, 최근 급변 시 ewma > simple, λ 경계.

### 2.6 평가 정교화 — Lo 보정 SE + Deflated Sharpe

[server/app/engine/evaluation.py](server/app/engine/evaluation.py) 확장. 표본이 쌓인 뒤(N_days ≥ 60) 의미.

1. **Lo(2002) 자기상관 보정 SE** (Newey-West 형):
   ```
   SE_Lo = SE_iid × √( 1 + 2·Σ_{k=1..q} (1 − k/(q+1))·ρ_k ),   q = 5
   ρ_k = 일일 수익률의 k차 자기상관 = Σ(r_t−r̄)(r_{t−k}−r̄) / Σ(r_t−r̄)²
   ```
   ρ_k 합이 음수로 과도해 √ 안이 ≤0 이면 SE_iid 로 폴백(방어).
2. **Deflated Sharpe Ratio** (Bailey & López de Prado 2014) — "여러 번 시도한 것 중 최고"의 보정:
   ```
   PSR(SR*) = Φ( (SR − SR*)·√(N−1) / √(1 − γ₃·SR + ((γ₄−1)/4)·SR²) )
   SR* = √(V)·( (1−γ)·Φ⁻¹(1 − 1/K) + γ·Φ⁻¹(1 − 1/(K·e)) )
   ```
   - SR: **일간**(연환산 아님) 샤프. N: 수익률 표본수. γ₃: 왜도, γ₄: 첨도(정규=3).
   - V: 시도된 K 개 전략 SR 들의 분산 — 실무 근사로 `V = 1/(N−1)`.
   - γ = 0.5772156649(오일러-마스케로니). Φ/Φ⁻¹ 는 `statistics.NormalDist().cdf/inv_cdf`.
   - **K(시도 횟수)는 자동 측정 불가** — 설정 `EVAL_TRIALS_K`(기본 1)로 운영자가 정직하게 기입
     (파라미터 튜닝 1회 = K+1). 판정: `DSR = PSR(SR*) ≥ 0.95` 를 "유의성 충족"의 상위 기준으로.
   - 왜도/첨도: `γ₃ = m₃/m₂^1.5`, `γ₄ = m₄/m₂²` (mᵢ = i차 중심적률, 모집단 정의로 단순 계산).
3. verdict 사다리 확장: 기존 N<100 게이트 → σ=0 → |SR|<2·SE_Lo → DSR<0.95 → 충족.
4. **테스트**: 자기상관 0 데이터에서 SE_Lo≈SE_iid, 양의 자기상관에서 SE_Lo>SE_iid,
   DSR 은 K=1 vs K=10 에서 단조 감소, 정규 데이터 γ₃≈0/γ₄≈3.

---

## §3. P2 — 클라우드 자율 운용 (M2)

### 3.0 M2 재검토 결과 (2026-07-11) — 아키텍처·격차·비용·구현 순서

> 배경: 로컬 상시 가동이 불가한 상황 확정 → **페이퍼 운용 자체를 M2(클라우드)에서 개시**하는
> 것으로 순서 변경. 아래는 §3.1–3.8 원안을 배포 직전 시점에서 재검토한 결과.

**최종 아키텍처** (asia-northeast3 — 서울. 국내 증권사 API 의 해외 IP 차단 관행 대비):
```
Cloud Scheduler(잡 2개, OIDC) ──POST──▶ Cloud Run(request-based, min=0/max=1) ──▶ Supabase PG(세션 풀러)
  ① tick:   */5 9-15 * * 1-5 (Asia/Seoul)  → /internal/tick                       (AWS 서울 — §아래 DB 결정)
  ② report: 30 16 * * *      (Asia/Seoul)  → /internal/report?force=false (휴장일에만 실생성 — §3.9)
아웃바운드: 토스 API · Anthropic · Telegram
```

**재검토에서 확인된 격차 — 로컬과 달라지는 것**:

| # | 항목 | 대응 |
|---|---|---|
| 1 | 내장 틱 루프 사용 불가(min=0 — 요청 밖 CPU 스로틀·인스턴스 수시 종료) | `TICK_INTERVAL_SEC=0`(이미 기본값) + Scheduler 잡 ① |
| 2 | **휴장일 보고서 트리거가 내장 루프 안에만 있음** → 클라우드에선 죽은 코드 | Scheduler 잡 ② + `/internal/report?force=false` maybe 경로 신설(§3.9) |
| 3 | **컨테이너 FS 휘발** → `reports/*.md` 유실 + 비루트(runner)가 `/app/reports` mkdir 실패 → 500 | 보고서 본문을 DB 정본으로(§3.9), 파일 저장은 best-effort 강등 |
| 4 | 틱 소요시간(조사 ≤5콜 + 판단 ≤10콜 직렬 = 수 분) > Scheduler 기본 deadline 3분 | `attempt_deadline=900s` · Cloud Run timeout 900s · `retry_count=0`(다음 파이어가 커버, 중복은 락) |
| 5 | 콜드 스타트 시 엔진 상태 | **이미 대응됨** — 킬스위치·CB 래치·원장·캔들 캐시 전부 DB 복원 설계 |
| 6 | 토스 API 가 데이터센터 IP 를 차단할 가능성(미확인 — BASIC tier 문서에 명시 없음) | 서울 리전 + **배포 직후 1단계 검증**(§3.2 말미): 토스 인증·조회 성공 확인 후에만 Scheduler 활성화 |
| 7 | `APP_ENV=production` 판별 부재(§3.7 하드닝·경로 분기의 전제) | §3.7 과 함께 `APP_ENV` 설정 도입(POSIX `ENV` 예약어 회피) |

**예상 비용 (월, asia-northeast3 — 2026-07 단가 기준 추정치, 청구 전 콘솔 재확인)**:

| 항목 | 산정 근거 | 월 예상 |
|---|---|---|
| Cloud Run | 1 vCPU/1GiB request-based. 틱 ~1,700회/월 × 1–8분(LLM 대기 포함 과금) − 무료구간 180k vCPU-s | **$2–12** |
| DB — Supabase 무료 티어 | AWS 서울(ap-northeast-2)·500MB·세션 풀러 (**채택 — 아래 결정**) | **$0** |
| Scheduler·Artifact Registry·Secret Manager·Logging·egress | 잡 2개(3개까지 무료)·이미지<0.5GB·시크릿 6·로그<50GiB | **$0–1** |
| **GCP 소계** | (Cloud SQL 전환 시 +$11–13) | **≈ $2–13** |
| Anthropic 판단 (Opus, 프롬프트 캐시) | 실질 150–400콜/일(상한 400) × 입력 ~3k/출력 ~0.3k tok | **$40–120** |
| Anthropic 조사 (web_search) | 캐시 없으면 최대 ~390콜/일 — **비용 지배 항목** | **$80–300** → §3.10 캐시 도입 시 **$10–40** |
| **총계 (§3.10 포함)** | | **≈ $50–170/월** |

- LLM 비용은 클라우드 이전과 무관하게 로컬에서도 동일하게 발생 — 절감 노브:
  §3.10(최우선) → `TICK_INTERVAL` 5→10분(전체 ½) → `RESEARCH_TOP_N`/`JUDGE_TOP_N` 축소.
- **DB 결정(2026-07-11)**: Supabase 무료 티어 채택(비용), 단 **Cloud SQL 전환을 상시 대비**:
  - 코드는 `DATABASE_URL` 문자열 하나로 추상 — 전환 시 코드 변경 0. Terraform 에는 Cloud SQL
    모듈을 `var.enable_cloud_sql=false` 스텁으로 유지(§3.2), 전환 = 변수 토글 + 시크릿 교체.
  - 전환 절차(장외에): `pg_dump` → Cloud SQL 복원 → DATABASE_URL 시크릿 새 버전 → 재배포.
    **LIVE(M3) 진입 전에는 반드시 전환**(실자금 원장을 무료 서드파티에 두지 않는다).
  - **함정**: Supabase 직결 호스트(db.\<ref\>.supabase.co)는 IPv6 전용 ↔ Cloud Run 이그레스는
    IPv4 → **Supavisor 세션 모드**(`aws-0-ap-northeast-2.pooler.supabase.com:5432`, 사용자
    `postgres.<ref>`)를 쓴다. 트랜잭션 모드(6543) 금지 — asyncpg prepared statement 와
    §3.4 advisory lock(세션 귀속)이 깨진다.
  - 무료 티어 "7일 무활동 일시정지"는 잡 ②(매일 DB 조회)가 자연 방지.

**구현 순서 (커밋 단위 — W6 전까지는 전부 로컬에서 테스트 가능)**:

| 순번 | 내용 | 성격 |
|---|---|---|
| W1 ✅ | §3.7 하드닝(`APP_ENV=production` 도입·docs 차단·기본키 기동 거부) + §1.3 CB 수동 리셋 엔드포인트(원격 운용 필수 도구) | 코드 |
| W2 ✅ | §3.9 보고서 클라우드 영속 + maybe 트리거 경로 | 코드 |
| W3 ✅ | §3.3 OIDC 검증 | 코드 |
| W4 ✅ | §3.4 PG advisory lock | 코드 |
| W5 | §3.1 Dockerfile(+컨테이너 스모크) + §3.8 CI(3.12 고정) | 빌드 |
| W6 | §3.2 Terraform + 시크릿 주입(운영자) + 배포 → **1단계 검증(토스 IP)** → Scheduler 활성화 | 인프라 |
| W7 | §3.10 조사 캐시(LLM 비용 절감 — 권장) | 코드 |

운영자 준비물(W6 전): GCP 프로젝트+결제 계정, `gcloud` CLI 인증, **Supabase 무료 프로젝트
생성(리전 서울)**, 시크릿 값 6종(토스 2·Anthropic·API_KEY·DATABASE_URL(Supabase 세션 풀러)·
텔레그램 토큰), KRX 2026 휴장일 공지 검증(§3.6 잔여).

### 3.1 Dockerfile (+ .dockerignore)

```dockerfile
# server/Dockerfile
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml ./
COPY app ./app
COPY data ./data                 # KRX 시드(SYMBOL_SOURCE_PATH=data/krx_symbols.json)
RUN pip install --no-cache-dir .
RUN useradd -m runner
USER runner
# Cloud Run 은 $PORT 를 주입한다 — 8080 기본
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
```
`.dockerignore`: `.venv/ tests/ scripts/ *.db __pycache__/ .pytest_cache/ *.egg-info/ .env*`.
scripts 는 이미지에 불필요(진단은 로컬). **주의**: `.env` 류가 이미지에 절대 들어가지 않게 확인.

재검토 추가(§3.0):
- **비루트 쓰기**: `/app` 은 runner 소유가 아니다 — 보고서는 §3.9 로 DB 가 정본,
  클라우드 env 는 `REPORTS_DIR=/tmp/reports`(그마저 실패해도 §3.9 가 warning 강등).
- **파이썬 버전**: 로컬 3.14 / 이미지 3.12 — pyproject `>=3.12` 이므로 CI(§3.8)를 3.12 로
  고정해 이미지와 정합(로컬-이미지 차이를 CI 가 조기 검출).
- **빌드 스모크**: `docker build` 후 `docker run -e API_KEY=test -p 8080:8080` → `/health` 200 확인
  (DB·토스 미설정 상태로도 기동해야 한다 — 기존 옵셔널 설계 그대로).

### 3.2 Terraform 리소스 명세 (`infra/`)

| 리소스 | 필수 설정 | 함정 |
|---|---|---|
| `google_artifact_registry_repository` | `asia-northeast3`, docker | |
| `google_secret_manager_secret` ×6 | TOSS_CLIENT_ID/SECRET · ANTHROPIC_API_KEY · API_KEY · DATABASE_URL · NOTIFY_TELEGRAM_BOT_TOKEN | 값은 TF 밖에서 주입(`gcloud secrets versions add`) — state 에 비밀 금지. chat_id 는 평문 env 가능 |
| `google_sql_database_instance` — **스텁**(`var.enable_cloud_sql=false`, 기본 미생성) | 전환 대비만: PG16, `asia-northeast3`, db-f1-micro 급, 삭제 보호 on | DB 는 Supabase(§3.0 결정). 전환 = 변수 토글 + DATABASE_URL 시크릿 교체 |
| `google_cloud_run_v2_service` | env: secret refs + `APP_ENV=production`·`REPORTS_DIR=/tmp/reports`·`TICK_INTERVAL_SEC=0`. `min_instance_count=0`, **`max_instance_count=1`**(§3.4 전까지 동시성 상한이 곧 안전장치), **timeout 900s**(틱이 수 분 — §3.0-4) | DB 연결: DATABASE_URL 시크릿 직결(Supabase 세션 풀러 — §3.0 함정 참조). Cloud SQL 전환 시 `run.googleapis.com/cloudsql-instances` annotation + unix socket 으로 교체. **`--no-allow-unauthenticated`** — 플랫폼 IAM 이 1차 방벽(invoker = scheduler-sa + 운영자 계정만), 앱 인증(OIDC/API키)은 2차 |
| `google_service_account` ×2 | run-sa(Secret accessor·SQL client), scheduler-sa(run invoker) | 최소 권한 |
| `google_cloud_scheduler_job` ×2 | ① tick `*/5 9-15 * * 1-5` ② report `30 16 * * *` — 둘 다 **`time_zone="Asia/Seoul"`**(UTC 환산 불필요·오프바이원 예방), HTTP POST + **OIDC token**(scheduler-sa, audience=run_url) | **`attempt_deadline="900s"`**(기본 3분이면 틱 도중 잘림)·`retry_count=0`(다음 파이어가 커버 — 재시도는 중복 파이어만 만든다, 락이 직렬화하지만 무의미). 15:30 초과분·휴장일은 서버가 거른다(§3.6) |

DATABASE_URL(예) — Supabase(현행):
`postgresql+asyncpg://postgres.<ref>:pw@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres`
· Cloud SQL 전환 시: `postgresql+asyncpg://user:pw@/dbname?host=/cloudsql/PROJECT:asia-northeast3:INSTANCE`
(asyncpg 는 유닉스 소켓을 `host=` 쿼리로 받는다 — 콜론 경로 그대로).

운영자 수동 호출(--no-allow-unauthenticated 이후): IAM 토큰 + 앱 API키 이중 헤더 —
`curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" -H "X-API-Key: …" {run_url}/api/status`.

**배포 직후 1단계 검증 (Scheduler 활성화 전 — §3.0-6 토스 IP 리스크)**:
1. `/health` 200 → `/api/status` (DB·모드·CB 상태 확인)
2. `/api/holdings` — **토스 인증+조회가 클라우드 IP 에서 성공하는지**가 관문.
   401/403/차단 시: 토스 Open API 지원 채널에 IP 정책 문의(고정 IP 필요 시 서버리스 VPC 커넥터
   + Cloud NAT 고정 IP 경로 — 비용 +$10~/월, 필요 확정 전 도입 금지)
3. `POST /internal/tick` 수동 1회(장중) → 응답 노트·DB 기록·텔레그램 수신 확인
4. 이상 없으면 Scheduler 잡 2개 enable

### 3.3 ✅ `/internal/tick` OIDC 검증 (구현됨 — /internal/report 도 동일. Bearer 제시 시 API 키 폴백 차단)

의존성 `google-auth` 추가. [server/app/api/deps.py](server/app/api/deps.py):
```python
async def require_tick_auth(request: Request, x_api_key: str | None = Header(default=None),
                            authorization: str | None = Header(default=None),
                            settings: Settings = Depends(get_settings)) -> None:
    """Scheduler(OIDC Bearer) 또는 로컬(API 키) 이중 경로. 설정된 쪽만 통과."""
    if authorization and authorization.startswith("Bearer ") and settings.oidc_audience:
        token = authorization.removeprefix("Bearer ")
        claims = await run_in_threadpool(          # verify 는 동기 — 스레드풀로
            id_token.verify_oauth2_token, token,
            google_requests.Request(), settings.oidc_audience)
        if claims.get("email") == settings.scheduler_sa_email and claims.get("email_verified"):
            return
        raise HTTPException(401, "OIDC 토큰 검증 실패")
    # 폴백: 기존 API 키(로컬/수동)
    if x_api_key and secrets.compare_digest(x_api_key, settings.api_key):
        return
    raise HTTPException(401, "인증 실패")
```
설정: `OIDC_AUDIENCE`(Cloud Run URL), `SCHEDULER_SA_EMAIL`. **함정**: verify 는 구글 공개키를
HTTP 로 가져온다(내부 캐시 있음) — 네트워크 실패 시 401 이 아니라 500 나지 않게 try/except → 401.
테스트: 목 claims 로 성공/이메일 불일치/만료 경로(monkeypatch `verify_oauth2_token`).

### 3.4 ✅ PG advisory lock — 다중 인스턴스 틱 직렬화 (구현됨 — db/lock.py)

현행 asyncio.Lock 은 프로세스 내부용. Cloud Run 인스턴스가 2개 뜨면 무력 →
`max_instance_count=1`(§3.2)이 1차 방어, advisory lock 이 정식 해법.

**함정이 많다 — 정확히 이렇게**:
```python
# server/app/db/lock.py (신규)
TICK_LOCK_KEY = 0x544F5353            # 임의 고정 상수("TOSS")

@asynccontextmanager
async def pg_tick_lock(engine) -> AsyncIterator[bool]:
    """True = 락 획득. advisory lock 은 '커넥션'에 묶인다 — 같은 커넥션을 끝까지 유지해야 한다."""
    if engine.dialect.name != "postgresql":
        yield True                     # SQLite(로컬) — in-process asyncio.Lock 이 이미 직렬화
        return
    async with engine.connect() as conn:              # 풀에서 1개 점유, 블록 끝까지 유지
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": TICK_LOCK_KEY})).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TICK_LOCK_KEY})
```
- **try(비블로킹)** 을 쓴다: 이미 도는 틱이 있으면 스킵(현행 asyncio 락과 같은 시맨틱).
- unlock 을 **같은 conn** 에서 — 풀 반환 후 다른 커넥션으로 unlock 하면 실패한다.
- 커넥션 끊기면 PG 가 자동 해제(크래시 안전).
- 통합: `execute_tick` 의 asyncio 락 안쪽에서 `async with pg_tick_lock(app.state.db_engine) as got:`
  → `not got` 이면 `{"skipped": "다른 인스턴스가 틱 실행 중"}`.
- **Supabase 주의**: advisory lock 은 세션 귀속 — 세션 모드 풀러(5432)에선 정상,
  트랜잭션 모드(6543)에선 문장마다 커넥션이 바뀌어 무의미(§3.0 DB 결정에서 세션 모드 강제).
- 테스트: SQLite 경로(항상 True), PG 는 통합환경 없으면 dialect 분기만 단위 테스트 + 문서화.

### 3.5 ✅ 알림 채널 (텔레그램) — P1 로 승격되어 구현됨

`server/app/core/notify.py` (신규):
```python
class Notifier(Protocol):
    async def send(self, text: str) -> None: ...
class NullNotifier: ...                                  # 미설정 시
class TelegramNotifier:
    # POST https://api.telegram.org/bot{token}/sendMessage  json={"chat_id":…, "text":…}
    # timeout 5s. 실패는 log.warning 후 삼킨다 — 알림 실패가 틱을 죽이면 안 된다.
```
설정: `NOTIFY_TELEGRAM_BOT_TOKEN`, `NOTIFY_TELEGRAM_CHAT_ID`. lifespan 에서 `app.state.notifier` 조립.

**발화 지점과 전이 감지**(스팸 방지가 설계의 핵심):
| 이벤트 | 감지 방법 | 중복 억제 |
|---|---|---|
| 서킷브레이커 발동/해제 | tick.py 에서 assess 전후 `tripped` 비교(**전이만**) | 전이 기반이라 불필요 |
| 리컨실 불일치 | `report.ok == False` | discrepancy 목록의 `hash(frozenset(symbol,kind))` 를 메모리에 보관, 같은 해시는 60분 억제 |
| 자동 틱 예외 | tick_loop except 블록 | 같은 예외 문자열 60분 억제 |
| 킬스위치 변경 | kill-switch 라우트 + 리컨실 자동 발동부 | 전이 기반 |
| LIVE 주문 제출/실패 | §4 에서 submit 결과 status 별 | 없음(전부 통지) |
비밀·계좌번호를 메시지에 절대 포함하지 않는다. 테스트: 목 Notifier 로 전이/억제 로직.

### 3.6 ✅ KRX 휴장일 캘린더 — 구현됨(2026 목록은 KRX 공지 검증 필요)

- 데이터: `server/data/krx_holidays.json` — `{"2026": ["2026-01-01", "2026-01-28", …]}`.
  출처는 KRX 공지(연 1회 수동 갱신 — fetch 자동화는 KRX OTP 절차가 번거로워 보류. 갱신 절차를
  파일 헤더 주석에). **파일에 해당 연도가 없으면 경고 로그 + 평일=거래일 폴백**(조용한 실패 금지).
- `app/core/calendar.py`: `load_holidays() -> frozenset[str]`, `is_trading_day(d: date, holidays) -> bool`.
- 통합 2곳: `GuardrailConfig.holidays: frozenset[str]`(guard_market_hours 에서 `n.date().isoformat() in`
  검사 추가) + `tick.py in_market_hours`. 설정 로드는 lifespan 1회.
- 테스트: 휴일 주문 차단, 연도 누락 폴백, 평일 정상.

### 3.7 ✅ 보안 하드닝 (구현됨 — APP_ENV=production 시 docs 차단·기본 API키 기동 거부)

점검 결과 HIGH/MEDIUM 0건. 아래 2건은 로컬(127.0.0.1)에선 무해하나 **Cloud Run 배포 시 필수**:

1. **`/docs`·`/openapi.json` 무인증 노출 차단** — FastAPI 기본값이 라우트 맵(킬스위치 경로·
   `X-API-Key` 헤더명 포함)을 무인증 열람 허용. 운영에선 정찰 보조.
   구현: `create_app()` 에서 운영 판별(`APP_ENV=production` 설정) 시
   `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`. 로컬 기본은 유지(개발 편의).
2. **기본 `API_KEY="dev-local-key"` 기동 거부(fail-closed)** — 현재 경고만 내고 구동됨.
   배포에서 `API_KEY` 설정 누락 시 문서화된 기본값으로 킬스위치 해제·보유 조회 가능해지는 클래스.
   구현: lifespan 에서 `APP_ENV=production AND api_key == "dev-local-key"` → RuntimeError 로 기동 중단
   (§1.1 LIVE-DB 강등과 같은 철학 — 단, 이번엔 강등이 아니라 거부: 조용한 노출이 더 위험).
   테스트: production+기본키 → 기동 실패, 로컬+기본키 → 경고만.

### 3.8 CI + 구조화 로깅

- `.github/workflows/ci.yml`: push/PR → `pip install -e ".[dev]"` → `ruff check app scripts tests`
  → `pytest -q`. (배포 잡은 인프라 안정 후.)
- 로깅: `LOG_FORMAT=json` 이면 stdlib `logging.Formatter` 를 JSON 포매터로 교체(자체 30줄 구현
  — 의존성 추가 없이). 필드: ts·level·logger·message·tick_id(있으면). Cloud Logging 이 severity 를
  집도록 `severity` 필드 포함.

### 3.9 ✅ 보고서 클라우드 영속 — 본문 DB 저장 + maybe 트리거 라우트 (구현됨)

**문제 2건**: (a) `generate_report` 가 markdown 을 컨테이너 FS 에만 쓰고 DB 엔 경로만 기록
— Cloud Run FS 는 휘발이라 본문 유실(비루트 권한으로 mkdir 실패 → 500 가능성도).
(b) 휴장일 자동 트리거(`maybe_generate_report`)가 내장 틱 루프 안에만 있어 클라우드(루프 OFF)에선
호출 경로가 없고, 기존 `POST /internal/report` 는 force=True 라 매일 cron 을 걸면 거래일에도 중복 생성.

**구현**:
1. `ReportLogRow.body: Mapped[str | None]`(Text, nullable — 기존 행 호환) +
   `record_report(period_end, path, body)` 저장. **DB 가 정본**.
2. 파일 저장(`reports_dir`)은 try/except 로 best-effort 강등 — 실패 시 `log.warning` 후 계속
   (텔레그램 요약·DB 기록은 진행).
3. 조회 라우트(API키 인증): `GET /api/reports`(목록 — period_end·generated_at),
   `GET /api/reports/{period_end}`(본문 markdown).
4. `POST /internal/report?force=false`(기본값을 false 로 변경) → force=False 는
   `maybe_generate_report` 시맨틱(휴장일 검사 + period_end 중복 방지 — 거래일엔 no-op).
   수동 즉시 생성은 `?force=true` 로 기존 동작 유지. Scheduler 잡 ②는 force=false 를 매일 호출.
5. **테스트**: body 왕복, 파일 저장 실패에도 성공(reports_dir 를 파일로 막아 강제),
   force=false 가 거래일 no-op/휴장일 생성, force=true 하위호환.

### 3.10 조사(web_search) 결과 TTL 캐시 — LLM 비용 지배 항목 절감 (권장)

**문제(§3.0 비용표)**: 조사는 심볼당 **매 틱** 재실행될 수 있다 — 최대 5콜/틱 × 78틱 = 390콜/일.
web_search 단가 + 검색결과 토큰(콜당 수천)이 전체 LLM 비용의 지배 항목. 일봉 전략에서 같은
심볼을 하루에 여러 번 조사할 정보 가치는 낮다.

**구현** — §2.1 캔들 캐시와 동일 패턴(주입형 래퍼, pipeline 무변경 §0-6):
1. DB `research_cache(symbol UNIQUE, summary TEXT, sources_json TEXT, fetched_at DateTime)`.
2. tick.py 조립부에서 research 러너를 DB-backed 캐시로 래핑:
   TTL 내 → 캐시 반환(연구 노트에 "캐시됨 HH:MM" 표기 — LLM 이 신선도를 알게), 경과 → 실조사 후 upsert.
3. 설정 `RESEARCH_CACHE_TTL_MINUTES=1440`(기본 1거래일. 0=비활성).
   **이원화 옵션**: 보유 종목(매도 판단)은 뉴스 신선도가 중요 → `RESEARCH_CACHE_HELD_TTL_MINUTES=120`.
4. 효과: 390콜/일 → 유니버스 로테이션 순증분(대략 40–80콜/일) — 조사 비용 ~80% 절감.
5. **테스트**: TTL 내 실조사 미호출(콜 카운터), 경과 후 재조사, 보유/비보유 TTL 분기, repo 없으면 생략.

---

## §4. P3 — LIVE 전환 (M3) — 페이퍼 평가 게이트(N≥100·유의성) 통과 후에만

### 4.1 OrderService.submit 비동기 전환 (선행 리팩토링)

토스 주문 전송은 async(httpx) — 현행 `submit` 은 sync 이고 executor Protocol 도 sync.
**전환 절차(전 호출부)**:
1. `OrderExecutor.place` → `async def place(self, order) -> str`. `CallableExecutor` 도 async fn 래핑.
2. `OrderService.submit` → `async def`. 내부 `self._executor.place(order)` → `await`.
3. 호출부 수정: [pipeline.py](server/app/engine/pipeline.py) 주문 루프 `order_service.submit(...)` → `await`.
4. 테스트 수정: `tests/test_order_guardrails.py` 의 submit 호출 함수들을 `async def` 로(asyncio_mode
   auto 라 데코레이터 불필요), `CallableExecutor(lambda…)` → async 람다 불가 → 헬퍼 `async def place(o)`.
   `tests/test_pipeline.py`·`test_api.py`·`test_db.py` 는 run_tick 경유라 무변경.
5. **불변식 재확인 테스트**: DRY_RUN 에서 executor 미호출(기존 `test_dry_run_never_calls_executor`)이
   async 전환 후에도 통과해야 하며, 이 테스트를 깨는 어떤 우회도 금지(§0-4).

### 4.2 주문 전송/조회/취소 클라이언트

[TOSS-AI-TRADING-INSIGHTS.md](TOSS-AI-TRADING-INSIGHTS.md) §2.4 기준(구현 전 openapi.json 재대조 — §0-7):
```python
# TossClient 추가 메서드 (모두 account=True 헤더)
async def place_order(self, body: dict) -> OrderAck:      # POST /orders — to_toss_body() 사용
async def get_order(self, order_id: str) -> OrderInfo:    # GET /orders/{id} — 상태·체결 수량
async def cancel_order(self, order_id: str) -> None:      # POST /orders/{id}/cancel (POST! 함정 4)
```
- **응답 모델은 실응답 확정 전 `extra="allow"` 원시에 가깝게** 두고, 첫 소액 주문의 실응답을
  픽스처로 저장한 뒤 필드를 조인다(스모크 우선 — 단, 주문은 실자금이라 "스모크"가 곧 첫 파일럿.
  절차: §4.6).
- `TossOrderExecutor(client)` : `place()` 에서 `client.place_order(order.to_toss_body())`,
  토스 orderId 반환. **재시도 금지**(멱등키가 있어도 전송 계층 재시도는 이중 주문 위험 —
  타임아웃 시 상태 조회로 확인하는 §4.3 경로만 허용. `_send_with_retry` 의 RETRYABLE 에서
  주문 POST 는 제외하는 플래그 필요 — `retryable=False` 파라미터).

### 4.3 체결 추적(fills) — 리컨실 정밀화·실 P&L

1. DB `fills(id, toss_order_id, client_order_id, symbol, side, filled_qty TEXT, fill_price TEXT,
   fee TEXT, filled_at, raw_json TEXT)`.
2. 틱 시작부(리컨실 전에!): 직전 미종결 주문(`orders.status == SUBMITTED` 이고 fills 미완결)을
   `get_order` 로 폴링 → 체결분 fills 기록, 전량 체결/취소/만료면 orders.status 갱신
   (`FILLED`/`CANCELLED` — OrderStatus enum 확장).
3. **리컨실 개선**: `submitted_qty_since` → `filled_qty_since`(fills 기준). 미체결은 이제 기대에
   포함되지 않으므로 부분체결 오탐 해소. 전환 스위치: fills 테이블 도입 후 리컨실 소스 교체.
4. 일일 매수 사용액: LIVE 에선 submitted 기준 유지(보수 — 체결 전 노출도 예약으로 간주).
   DRY_RUN 기록과의 혼산은 모드 필터(`orders.mode == 현재 모드`)로 정리 — **테스트**: 모드 전환일
   시나리오(어제 DRY_RUN 기록이 오늘 LIVE 한도를 잠식하지 않는지, 같은 날 전환 시 보수 합산 유지).

### 4.4 LIVE 성과 평가 (실 equity 곡선)

페이퍼와 동일 파이프를 실계좌에: 틱마다 `실 equity = 예수금(buying_power) + Σ holdings 평가(KRW)`
를 `paper_equity` 와 같은 스키마의 `live_equity` 테이블에 기록(벤치마크 동시).
`/api/evaluation?source=live|paper`(기본 paper). n_trades 는 fills 의 SELL 완결 수.
§1.3 입출금 왜곡이 여기도 적용 — TWR 정규화 전까지 입출금 시 수동 리셋 절차.

### 4.5 롤아웃 절차 (기계적으로 따를 것)

1. 페이퍼 게이트 확인: `/api/evaluation` — N≥100 · verdict "충족" · MDD 허용범위.
2. 한도 축소: `PER_ORDER_MAX_KRW=30000 · DAILY_BUY_CAP_KRW=60000 · MAX_POSITIONS=3` 로 시작.
3. `TRADING_MODE=LIVE` + `I_UNDERSTAND_LIVE_REAL_MONEY=YES` (process env — §1.1 로 DB 필수).
4. **첫 주문은 장중 수동 감시** 하에 1건: 알림·orders/fills·리컨실(다음 틱 OK) 확인.
   실응답 픽스처 저장 → 모델 필드 확정 커밋.
5. 1주일 무사고(리컨실 OK·서킷브레이커 미발동) 후 한도 단계 상향. 사고 시 킬스위치 → 원인 분석
   문서화 전 재개 금지.

---

## §5. P4 — 정교화(장기)

- **TWR(시간가중수익률)**: 입출금 이벤트 테이블 → 곡선 정규화(§1.3 정석 해법).
- **포트폴리오 관점 판단**: 후보별 독립 판단 → 후보 전체를 한 프롬프트로 랭킹(교차 비교).
  트레이드오프: 토큰↑·스키마 복잡 vs 상대 비교 가능. 배치 스키마
  `{"rankings":[{symbol, action, confidence, rationale}...]}` + 후보 수 상한 필수.
- **confidence 계단 사이징**: §2.3 캘리브레이션 결과가 비단조면 `conf<0.6 → 0 · 0.6~0.8 → 0.5 ·
  >0.8 → 1.0` 계단으로 교체(allocator 한 줄).
- **해외주(USD) FX 정규화**: 서킷브레이커 equity·가드레일 한도가 KRW 버킷만 봄 — 환율 API 도입
  전까지 **KR 전용 운용을 명시**(유니버스가 KRX 시드라 자연 충족. 실계좌에 USD 보유가 있으면
  equity 과소평가 → HWM 왜곡 방향은 보수적이나, 리컨실은 심볼 단위라 무관).
- 데스크톱 앱(Tauri) — M2 후. OpenAPI → openapi-typescript 타입 생성 플로우는 TECH-STACK §3.

## §6. 우선순위 총괄

| 순위 | 항목 | 근거 |
|---|---|---|
| **P0** | ✅ §1.1 LIVE-DB 강제 · ✅ §1.2 결정적 청산 (§1.3 은 LIVE 직전) | 극단 손실 방지 직결. §1.2 는 페이퍼 회전율(표본 축적)에도 필수 |
| **P1** | ✅ §2.1 캔들 캐시 · ✅ §3.5 알림 · ✅ §2.3 캘리브레이션 · ✅ §2.2 ADV 선별 | 완료 |
| **P2** | §3 배포 세트 — 순서는 §3.0 W1~W7: (3.7+1.3) → 3.9 → 3.3 → 3.4 → (3.1+3.8) → 3.2 → 3.10 | **2026-07-11 클라우드 우선으로 전환 확정**(로컬 상시 가동 불가) — 페이퍼 운용 개시 자체가 M2 |
| **P3** | §4 전체 (순서: 4.1→4.2→4.3→4.4→4.5) | 게이트 통과 후에만 |
| **P4** | §2.4–2.6 · §5 | 표본이 충분해진 뒤 의미 |

## §7. 검토 완료 제안 — 샌드박스 시뮬레이션 · 휴장일 자동 보고서

### 7.1 ✅ 토스 API 없는 샌드박스 시뮬레이션 — 구현됨(stress_sim·backtest), LLM 알파 소급 평가는 불가

**구조적 근거**: 파이프라인은 이미 완전 주입형이다 — `run_tick(toss=…)` 은 덕타이핑(테스트의
FakeToss 가 증명), 페이퍼 모드는 holdings/현금을 합성으로 대체하며, `now` 도 파라미터다.
필요한 것은 **데이터 어댑터와 시뮬레이션 시계**뿐.

**티어 B — 합성 스트레스 샌드박스** (안전 검증, 효과/노력비 최고 — 먼저):
- `scripts/stress_sim.py`: GBM/부트스트랩 합성 가격 경로(폭락 −30%·갭·횡보 시나리오)를 주는
  `SyntheticToss` + 인메모리 페이퍼 장부로 `run_tick` 을 수백 틱 구동.
- **검증 대상 = 안전장치 체인**: "어떤 경로에서도 서킷브레이커가 −15% 부근에서 신규 진입을 멈추는가,
  손절이 포지션 손실을 −8%+슬리피지 내로 자르는가, 일일 한도·레짐 축소가 수식대로 동작하는가"를
  **시나리오 단위로 단언**(알파 평가 아님 — 판단기는 Deterministic/규칙 주입).
- 노력: 소(기존 test fixture 패턴 재사용). 산출: 시나리오별 최대낙폭·발동 시점 표.

**티어 A — 히스토리컬 리플레이(백테스트)** (결정적 전략 검증·파라미터 보정):
- 데이터: 토스 제외 조건이므로 외부 무료 소스(`pykrx` 또는 `FinanceDataReader`)로 KRX 일봉
  OHLCV 히스토리를 로컬 적재(`data/history/`). `ReplayToss` 가 시뮬레이션 시각 T 기준
  **point-in-time 슬라이스**만 서빙(T 이후 봉 노출 금지).
- 드라이버: FastAPI/HTTP 우회 — tick.py 의 소규모 오케스트레이션(페이퍼 로드→마킹→exits→run_tick
  →체결→equity)을 표준 루프로 복제한 `scripts/backtest.py`. 체결은 **다음 봉 시가**(같은 봉 종가
  진입은 미래정보 누출 — study.md §7.2 규율), 비용은 기존 CostConfig.
- 평가: 기존 `evaluation.evaluate`(Sharpe/MDD/벤치마크) 재사용 — 자산곡선 소스만 교체.
- **한계 3가지를 명시하고 시작할 것**:
  1. **LLM 판단은 소급 평가 불가** — 모델 훈련데이터가 과거 결과를 "기억"(look-ahead 오염)하고
     web_search 는 현재 뉴스를 반환. LLM 알파는 **전방(페이퍼) 전용**. 리플레이는 결정적 구성요소
     (스크리너·게이트·레짐·exits·사이징)와 **파라미터 보정**(문턱·배수·손절폭)에만 유효.
  2. **생존편향** — 현재 KRX 시드는 상폐 종목 부재. 절대 성과는 상향 편향(파라미터 상대 비교엔 사용 가능).
  3. 체결 현실성 — §2.4 미체결 모형과 동일 가정 한계.
- 노력: 중(데이터 적재 스크립트 + ReplayToss + 드라이버 + 규율 테스트).

### 7.2 ✅ 휴장일 자동 보고서 — 구현됨(engine/report + /internal/report + 루프 트리거)

**데이터는 이미 전부 DB에 있다**: ticks(레짐·차단 내역)·decisions(rationale·판단가)·orders·
paper_equity(곡선·벤치마크)·audit_log(안전 이벤트)·symbol_stats. 평가/캘리브레이션 함수도 재사용.

1. **생성기** `engine/report.py`(순수) + `scripts/` 또는 `/internal/report`:
   기간(직전 보고 이후) 요약 마크다운 — 자산곡선·evaluate() 지표·체결/강제청산 목록·
   비용게이트/레짐 차단 통계·안전 이벤트·캘리브레이션 표(§2.3 재사용)·다음 주 유니버스 풀 변화.
2. **트리거**: 내장 틱 루프의 "장외 continue" 분기에서 — `휴장일(주말·§3.6 공휴일) AND
   마지막 보고일 < 마지막 거래일` 이면 1회 생성(중복 방지 마커는 `engine_state` 또는 `report_log`
   테이블). 운영(M2)은 같은 로직을 daily cron + `/internal/report` 로.
3. **전달**: `server/reports/YYYY-MM-DD.md` 저장 + **텔레그램 요약**(§3.5 재사용 — sendMessage
   4,096자 제한이므로 핵심 지표만, 전문은 파일. 필요 시 sendDocument 로 파일 첨부).
4. (선택·후순위) LLM 내러티브: 통계를 Fable 5 에게 요약시킨 주간 코멘터리 — 비용가드 대상,
   결정적 통계 보고가 먼저.
- 의존: 공휴일 정밀 트리거는 §3.6(휴장일 캘린더) 선행이 자연스러움(주말 트리거는 즉시 가능).
