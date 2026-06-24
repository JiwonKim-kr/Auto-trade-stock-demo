# 토스증권 기반 AI 자동매매 — 인사이트 인계 문서

> **목적**: 프로토타입(`stock-node-graph`, Next.js)에서 얻은 인사이트를 **신규 프로젝트**
> (데스크톱 클라이언트 + GCP 클라우드 거래 서버, 스택 새로 작성)로 가져가기 위한 자기완결 문서.
> 특히 **토스 Open API 구현 중 우리가 혼란했던 점**을 자세히 남겨 같은 실수를 반복하지 않는다.
>
> 모든 사실은 **2026-06, 실제 발급 키(tier BASIC)로 라이브 호출하며 실측·검증**한 것이다.
> 언어/프레임워크 중립으로 작성(토스 API는 HTTP 사실, 설계는 개념). 코드 예시는 TS이나 *예시일 뿐*.

---

## 1. 개요 · 무엇을 가져가고 무엇을 버리나

- **버린다**: 프로토타입의 토폴로지(공개 웹 대시보드 on Vercel). 계좌/보유가 public URL에
  노출되는 구조라 부적합. 코드 자체도 스택을 새로 짜므로 직접 재사용 안 함.
- **가져간다**:
  1. **토스 Open API의 실전 사실/함정** (§2) — 가장 값진 자산. 이게 없으면 신규에서 똑같이 헤맨다.
  2. **검증 방법론**(스모크 우선) (§4).
  3. **설계 개념**(provider/주문 가드레일/유니버스/AI 엔진/시각화) (§5).
  4. **GCP 아키텍처 권고** (§6).
- **신규 형태**: 클라우드 서버(두뇌, 토스 creds·DB·자율 거래) + 데스크톱 앱(눈, 현황 조회·제어).

---

## 2. 토스 Open API 실전 필드 가이드 (핵심)

### 2.0 스펙을 어디서 보나 (이걸 처음부터 했어야 했다)
- ✅ **OpenAPI JSON**: `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`
- ✅ **마크다운 레퍼런스**: `https://openapi.tossinvest.com/openapi-docs/latest/api-reference/README.md`
- ⚠️ `developers.tossinvest.com/docs` 는 **JS 렌더**라 그냥 fetch 안 됨 → `developers.tossinvest.com/llms.txt` 만 텍스트로 읽힘(개괄만).
- ⚠️ 스펙 파일은 **`openapi.` 호스트**에 있다. `developers.` 호스트의 같은 경로는 **404**.
- ⚠️ 서드파티 블로그/비공식 CLI는 **경로가 틀릴 수 있다**(아래 함정 1). 공식 스펙만 신뢰.

### 2.1 인증
- 방식: **OAuth2 client_credentials**.
- 토큰 발급: `POST https://openapi.tossinvest.com/oauth2/token`
  - 헤더 `Authorization: Basic base64(client_id:client_secret)`, `Content-Type: application/x-www-form-urlencoded`
  - 바디 `grant_type=client_credentials`
  - **이 엔드포인트만 `/api/v1` prefix가 없다.**
- 응답: `{ access_token, token_type, expires_in }`, 만료 **약 24시간**(JWT exp 기준). tier 예: `BASIC`.
- 모든 리소스 호출: `Authorization: Bearer <access_token>`.
- 키 발급처: 토스증권 PC 웹 → 설정 → Open API (client_id / client_secret).

### 2.2 ⚠️ 7대 함정 (우리가 실제로 막힌 지점)

1. **리소스 경로 prefix는 `/api/v1` 다 (`/v1` 아님).**
   블로그를 보고 `/v1/...`로 짰다가 전부 `404 {"error":{"code":"edge-blocked","message":"요청한 API 경로를 지원하지 않습니다."}}`.
   토큰은 발급되는데(=`/oauth2/token` 맞음) 리소스만 404면 prefix를 의심하라.

2. **모든 응답이 `{ "result": ... }` 로 감싸여 온다.** 최상위에서 배열/필드 찾지 말고 `result`를 먼저 벗겨라.

3. **`X-Tossinvest-Account` 헤더 값 = `accountSeq` (예: `1`), `accountNo`(계좌번호) 아님.**
   `GET /api/v1/accounts` → `result: [{ accountNo: "0000000000", accountSeq: 1, accountType: "BROKERAGE" }]`.
   계좌번호(`0000000000`)를 헤더에 넣으면 `400 {"code":"account-not-found","message":"해당 계좌번호를 찾을 수 없습니다."}`.
   **반드시 `accountSeq`(작은 정수)를 넣어라.** (오해를 두 번 했다 — 스펙에 "account sequence identifier"로 명시됨.)

4. **금액은 통화별 중첩 객체다 — 환산 합계가 아니라 통화 버킷 분리.**
   - 요약(holdings 루트)에서: `marketValue.amount = { krw: "224000", usd: "0.081451" }` 처럼
     **KRW 보유분과 USD 보유분이 별도 버킷**. 같은 돈을 두 통화로 표기한 게 아니다(합치려면 환율 환산 필요).
   - 개별 종목(item) 레벨에서는 금액이 **그 종목 통화의 평문 문자열**이고, `item.currency`가 통화를 알려준다.
   - `profitLoss.rate` 는 **분수**다: `"-0.0218"` = **-2.18%** → 표시하려면 ×100.
   - (이 차이 — 루트는 중첩 {krw,usd}, item은 평문 — 때문에 매핑이 한참 어긋났다.)

5. **`/api/v1/stocks` 는 `symbols` 파라미터 필수 (지정 조회). "전체 상장 종목 목록" 엔드포인트가 없다.**
   파라미터 없이 부르면 `400 {"code":"invalid-request", field: "symbols"}`.
   → **유니버스(전 종목)용 심볼 소스는 별도로 마련**해야 한다(KRX 종목 목록 등). 토스는 "지정 종목 정보 enrich"만.

6. **sandbox/모의투자 API가 없다 → 주문 호출 = 실시장·실자금.** 앱 내 모의투자는 API로 못 건드린다.
   - **WebSocket 공식 미공개** → REST 폴링(최대 1초).
   - `prices` 응답엔 **등락률/거래량이 없다** → 그건 `candles`/`trades`로 따로 구해야 함.

7. **주문 취소/정정은 POST다 (DELETE/PATCH 아님).** `POST /api/v1/orders/{id}/cancel`, `POST /api/v1/orders/{id}/modify`.
   **`buying-power` 는 `?currency=KRW` 필수** (없으면 `400 field: currency`).

### 2.3 엔드포인트 표 (확인됨, 모두 `Bearer` 필요; 계좌계열은 `X-Tossinvest-Account: <accountSeq>` 추가)

| 분류 | 메서드 · 경로 | 비고 |
|---|---|---|
| 인증 | `POST /oauth2/token` | prefix 없음. Basic auth, client_credentials |
| 계좌 | `GET /api/v1/accounts` | **헤더 불필요**. 여기서 accountSeq 획득 |
| 계좌 | `GET /api/v1/holdings` | 보유 + 요약. 헤더 필요 |
| 계좌 | `GET /api/v1/buying-power?currency=KRW` | currency 필수 |
| 계좌 | `GET /api/v1/sellable-quantity`, `GET /api/v1/commissions` | 매도가능수량 / 수수료 |
| 주문 | `POST /api/v1/orders` | 생성 |
| 주문 | `GET /api/v1/orders`, `GET /api/v1/orders/{id}` | 목록 / 상세 |
| 주문 | `POST /api/v1/orders/{id}/cancel`, `POST /api/v1/orders/{id}/modify` | **POST**(취소/정정) |
| 시세 | `GET /api/v1/prices?symbols=A,B` | 현재가. `lastPrice`(문자열). 등락률/거래량 없음 |
| 시세 | `GET /api/v1/orderbook`, `/candles`, `/trades`, `/price-limits` | 호가/캔들/체결/가격제한 |
| 종목 | `GET /api/v1/stocks?symbols=A,B` | **symbols 필수**. 마스터(섹터 없음) |
| 종목 | `GET /api/v1/stocks/{symbol}/warnings` | **종목별** 경고(전역 목록 없음) |
| 시장 | `GET /api/v1/market-calendar/KR`, `/US`, `GET /api/v1/exchange-rate` | 휴장일 / 환율 |

### 2.4 확정된 응답 형태 (실측 샘플)

**`GET /api/v1/accounts`**
```json
{ "result": [ { "accountNo": "0000000000", "accountSeq": 1, "accountType": "BROKERAGE" } ] }
```

**`GET /api/v1/holdings`** (루트=요약은 통화 버킷 중첩, items[]=종목별 평문)
```json
{ "result": {
  "totalPurchaseAmount": { "krw": "229000", "usd": "0.069972" },
  "marketValue":  { "amount": { "krw": "224000", "usd": "0.081451" }, "amountAfterCost": { "krw":"223552","usd":"0.081451" } },
  "profitLoss":   { "amount": { "krw": "-5000", "usd": "..." }, "rate": "-0.0217", "rateAfterCost": "..." },
  "dailyProfitLoss": { "amount": { "krw":"2000","usd":"..." }, "rate": "0.0087" },
  "items": [
    { "symbol":"005935","name":"삼성전자우","currency":"KRW","quantity":"1",
      "lastPrice":"224000","averagePurchasePrice":"229000",
      "marketValue":{ "purchaseAmount":"229000","amount":"224000","amountAfterCost":"223552" },
      "profitLoss":{ "amount":"-5000","amountAfterCost":"-5448","rate":"-0.0218","rateAfterCost":"-0.0237" },
      "cost":{ "commission":"0","tax":"448" } },
    { "symbol":"AAPL","name":"애플","currency":"USD","quantity":"0.000271",
      "lastPrice":"300.56","averagePurchasePrice":"258.199261",
      "marketValue":{ "purchaseAmount":"0.069972","amount":"0.081451" },
      "profitLoss":{ "amount":"0.011479","rate":"0.164" } }
  ]
}}
```
- `quantity`·`lastPrice`·금액 모두 **문자열**. 소수점 주문 지원(애플 0.000271주). `marketCountry` 필드도 있음("KR"/"US").

**`GET /api/v1/buying-power?currency=KRW`**
```json
{ "result": { "currency": "KRW", "cashBuyingPower": "0" } }
```

**`GET /api/v1/prices?symbols=005930`**
```json
{ "result": [ { "symbol":"005930","timestamp":"2026-06-22T19:59:59.000+09:00","lastPrice":"356500","currency":"KRW" } ] }
```

**`GET /api/v1/stocks?symbols=005930`** (섹터 없음. 위험 판정용 플래그가 풍부)
```json
{ "result": [ {
  "symbol":"005930","name":"삼성전자","englishName":"SamsungElec","isinCode":"KR7005930003",
  "market":"KOSPI","securityType":"STOCK","isCommonShare":true,"status":"ACTIVE","currency":"KRW",
  "listDate":"1975-06-11","delistDate":null,"sharesOutstanding":"5846278608","leverageFactor":null,
  "koreanMarketDetail":{ "liquidationTrading":false,"nxtSupported":true,"krxTradingSuspended":false,"nxtTradingSuspended":false }
} ] }
```

**주문 바디(`POST /api/v1/orders`)**: `clientOrderId`(멱등키), `symbol`, `side`(BUY/SELL),
`orderType`(LIMIT/MARKET), `quantity`, `price`, `orderAmount`, `timeInForce`(DAY/CLS).
(식별자는 `stockCode`가 아니라 **`symbol`**.)

### 2.5 기타 실측
- 개발 **자택 IP는 토스에 차단되지 않았다**(프로토타입의 Yahoo는 IP 429로 막혔던 것과 대조).
- ⚠️ 단, **BASIC tier 레이트 리밋은 있다**(2026-06 신규 프로젝트 실측): 지연 없이 빠르게 연속 호출하면 `429 {"error":{"code":"rate-limit-exceeded"}}`. IP 차단이 아니라 **호출 빈도 제한** → `/prices`·`/stocks`는 symbols **배치**로, 호출 간 페이싱·캐시, 429는 **백오프+Retry-After 재시도**.
- 토큰 만료 ~24h라 50분/주기 선갱신 같은 짧은 캐시는 사실 불필요할 수 있으나, 인메모리 캐시 + 만료 전
  갱신은 여전히 안전한 기본값.

---

## 3. 우리가 혼란했던 점 & 교훈 (프로세스)

| 무엇 | 왜 시간 낭비 | 교훈 |
|---|---|---|
| 블로그의 `/v1` 경로 신뢰 | 전부 404(edge-blocked), 여러 라운드 | **공식 openapi.json/README 먼저** 확보 후 코딩 |
| 응답 필드명 추측 | 실응답과 어긋나 매핑 0/빈값 | **스모크로 원시 응답 먼저 덤프** → 매핑 확정 |
| accountSeq vs accountNo | 둘 다 시도하며 왕복, env 이름까지 번복 | 식별자/헤더 값은 **스펙+실측으로 한 번에 확정** 후 명명 |
| 통화 중첩(루트 {krw,usd} vs item 평문) | 평면 숫자로 가정해 합계 왜곡 | 통화 모델을 먼저 파악(버킷 분리 + item.currency + rate는 분수) |

**한 줄 교훈: "공식 스펙 원본 → 스모크로 실응답 확인 → 매핑/코드" 순서를 지켜라. 추측 코드 금지.**

---

## 4. 빠른 검증 방법론 (신규에서 가장 먼저 할 것)

코드를 짜기 전에 **진단 스모크 스크립트**부터. 각 단계의 **원시 응답을 그대로 출력**해 필드를 눈으로 확정한다.
핵심은 **계좌 식별자 자동 판별**(accountSeq/accountNo 둘 다 시도)과 **주문 절대 미전송**.

```js
// 토큰 → accounts → (accountSeq/accountNo 자동 시도) holdings/buying-power → prices → stocks → warnings
// 각 단계 raw 덤프. 실행: node --env-file=.env smoke.mjs   (Node 20.6+)
const BASE = "https://openapi.tossinvest.com";
const tok = await (await fetch(`${BASE}/oauth2/token`, {
  method:"POST",
  headers:{ Authorization:`Basic ${Buffer.from(`${ID}:${SECRET}`).toString("base64")}`,
            "Content-Type":"application/x-www-form-urlencoded" },
  body:"grant_type=client_credentials" })).json();
const accounts = await (await fetch(`${BASE}/api/v1/accounts`, { headers:{ Authorization:`Bearer ${tok.access_token}` }})).json();
// accounts.result[0] 에서 accountSeq 와 accountNo 를 꺼내 holdings 에 차례로 시도 → 200 나오는 값이 정답
```
> 프로토타입의 `scripts/toss-smoke.mjs`가 이 패턴의 참조 구현(계좌 식별자 자동 판별 + 원시 덤프 + 주문 미전송).

---

## 5. 이식할 설계 개념 (코드 직접 재사용 X — 스택 새로 짬. 레포 파일은 참조 예시)

- **소스 어댑터/폴백 체인** (참조: `src/lib/providers/types.ts`의 `QuoteProvider`).
  시세 소스를 인터페이스 뒤로 추상화해 토스↔폴백 교체 가능하게.
- **주문 레이어 = 모드 게이트 + 하드 가드레일** (참조: `src/lib/toss/order.ts`).
  - `TRADING_MODE = DRY_RUN(기본) | LIVE`. DRY_RUN은 실 `POST /orders` 미호출, "의도된 주문"만 기록.
  - 진입 시 **하드 가드레일 선검사**: 킬스위치, 1주문 최대 금액(매수 비용 검증). 모드 무관 동일 적용.
  - **`clientOrderId` 멱등키**로 재시도 중복주문 방지.
  - 신규에서도 이 안전 골격은 **그대로 권장**.
- **유니버스 보수적 제외 = 마스터 실플래그로 정밀 판정** (참조: `src/lib/toss/universe.ts`).
  - 우선주 = `isCommonShare === false`, 레버리지/인버스 = `leverageFactor != null`,
    정리매매/거래정지 = `koreanMarketDetail.liquidationTrading|krxTradingSuspended`, 비활성 = `status !== "ACTIVE"`,
    SPAC/ETN = `securityType`(보조로 이름 정규식).
  - 종목별 데이터가 필요한 **경고(warnings)·저유동성/동전주**는 스크리너가 좁힌 **후보 단계에서 per-symbol** 적용(비용 절감).
  - **전 종목 심볼 소스는 외부(KRX 등)** — 토스로는 열거 불가(함정 5).
- **AI 엔진 = 하이브리드(2단계)**.
  - 결정적 기술지표 스크리너로 유니버스 → 소수 후보 압축(여기에 **LLM이 못 넘는 하드 가드레일** 배치).
  - Claude(`claude-opus-4-8`)가 후보에 한해 최종 BUY/SELL/HOLD + 사이징을 **구조화 출력(tool-use/JSON)**으로,
    **근거 텍스트 로깅**. 순수 LLM(비용·환각)·순수 규칙(경직) 대비 비용/감사/안전 균형.
  - 구현 전 `claude-api` 가이드 참조, 최신 Claude 모델 사용.
- **시각화(데스크톱에서 재구현 시 컨셉만)**: 허브-스포크 노드 그래프(보유 + AI 매매후보를 **시각 구분**),
  색상룰 **수익 빨강 / 손실 파랑 / 보합 회색**(한국 관습), **통화별 표기**(KRW "원", USD "300.56 USD") —
  해외 종목을 "원"으로 잘못 찍지 말 것(통화 라벨 필수).

---

## 6. 신규 아키텍처 권고 — GCP

```
 데스크톱 앱(눈)  ──(HTTPS + API키/IAP)──▶  Cloud Run (두뇌: API + 거래 로직)
   현황 조회·제어                                   │  ├─ Secret Manager (토스 client_id/secret)
   (킬스위치 등)                                     │  └─ Cloud SQL(PostgreSQL): 결정/주문/포지션/감사로그
                                                     ▲
                            Cloud Scheduler ─(장중 N분 cron)─▶ /tick (수집→스크리너→LLM→가드레일→주문)
```

- **토폴로지 원칙**: **토스 자격증명은 서버(Cloud Run)에만**. 데스크톱은 토스를 직접 안 부르고, 서버의 인증 API만 호출.
  공개 대시보드를 안 띄우므로 노출면이 작다(프로토타입의 public 노출 문제 원천 차단).
- **GCP 서비스 매핑(= 클라우드 학습 표면)**:
  - 서버/거래 로직 → **Cloud Run**(컨테이너). 이미지 → **Artifact Registry**.
  - 장중 주기 트리거 → **Cloud Scheduler → Cloud Run** (프로토타입에서 이미 사용: GCP 프로젝트
    `stock-node-graph-kr`, 리전 `asia-northeast3`, 매시 cron). 거래 틱을 장중 N분마다.
  - 시크릿 → **Secret Manager** (토스 creds·API키. env 하드코딩 금지).
  - DB → **Cloud SQL (PostgreSQL)** (관계형 모델에 적합). 단순화하려면 Firestore.
  - 데스크톱↔서버 인증 → **API 키**(서버 검증) 또는 **IAP(Identity-Aware Proxy)**(학습 보너스).
- **거래 루프 옵션(결정은 신규에서)**:

  | 옵션 | 방식 | 장점 | 단점 |
  |---|---|---|---|
  | 서버리스+스케줄러 | Cloud Run(min=0) + Scheduler N분 틱 | 저비용(유휴 0), 운영 단순 | 분 단위 반응(실시간 X), 콜드스타트 |
  | 상시 구동 | Cloud Run(min≥1) 또는 GCE VM 연속 루프 | 실시간/초단위 반응 | 비용↑, 운영 부담↑ |

  KR 주식·분 단위 전략이면 **서버리스+스케줄러로 충분**. 초단타면 상시.
- **안전(실자금 — 절대 양보 금지)**: `DRY_RUN→LIVE` **명시 전환**(다중 확인), 일일 매수 한도·종목당 비중·
  최대 포지션 수, **KRX 장시간 게이트**(09:00–15:30 KST), 멱등(`clientOrderId`), **토스 잔고와 리컨실**,
  **전 결정·주문 전수 감사로그**, 글로벌 **킬스위치**. LIVE 첫 전환은 **소액 1주**부터.

---

## 7. 신규 프로젝트 킥오프 체크리스트

1. 공식 **openapi.json / README** 확보(§2.0).
2. **스모크 스크립트** 먼저 — 토큰·계좌·보유·시세·종목 원시 덤프, accountSeq 자동 판별(§4).
3. 응답 **필드 매핑 확정**(§2.4; 통화 중첩·rate 분수 주의).
4. **DRY_RUN 주문 레이어 + 가드레일/킬스위치**(§5) — 실주문 0 보장부터.
5. **유니버스**(외부 심볼 소스 + 마스터 플래그 보수적 제외) → **스크리너** → **LLM 판단**(§5).
6. **GCP 인프라**: Secret Manager(토스 creds) → Cloud SQL → Cloud Run 배포 → Scheduler 틱(§6).
7. **데스크톱 뷰어**(서버 API/IAP로 현황·제어).
8. **LIVE 전환**: 소액 1주 → 점진 확대. 리컨실·감사로그로 검증하며.

---

## 부록 A. 프로토타입 참조 구현 위치 (개념 확인용)
- 토스 클라이언트/인증/시세/계좌/종목/유니버스/주문: `src/lib/toss/*`
- 응답 `result` 언래핑 헬퍼: `src/lib/toss/client.ts` (`unwrap`/`unwrapList`)
- 접근 게이트(개념): `src/proxy.ts` (Next 16: middleware→proxy)
- 그래프/색상/통화표기: `src/types/graph.ts`, `src/types/stock.ts`(`formatMoney`), `src/app/api/graph/route.ts`
- 스모크: `scripts/toss-smoke.mjs`

## 부록 B. 한 줄 요약 (절대 잊지 말 것)
1. 경로 prefix **`/api/v1`**, 응답 **`{result}`** 래핑.
2. 계좌 헤더 = **accountSeq(정수)**, 계좌번호 아님.
3. 금액 **통화 버킷 분리 + 문자열**, `profitLoss.rate`는 **분수(×100)**.
4. `/stocks`·`/prices` 는 **symbols 필수**, `buying-power`는 **currency 필수**, 취소/정정은 **POST**.
5. **sandbox 없음 = 실자금** → DRY_RUN·가드레일·킬스위치 먼저.
6. 전 종목 열거 불가 → **외부 심볼 소스** 필요.
