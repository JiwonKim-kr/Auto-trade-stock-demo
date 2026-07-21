# M2 클라우드 배포 (Terraform — PLAN §3.2)

아키텍처: Cloud Scheduler(OIDC) → Cloud Run(request-based, min=0/max=1) → **Supabase PG(세션 풀러)**.
DB 결정·비용·격차 배경은 [IMPLEMENTATION-PLAN §3.0](../IMPLEMENTATION-PLAN.md).

## 사전 준비 (운영자)

1. GCP 프로젝트 + 결제 계정, `gcloud auth login` + `gcloud config set project <PROJECT>`
2. **Supabase 무료 프로젝트(리전: 서울 ap-northeast-2)** 생성 → 연결 문자열은 반드시
   **세션 모드 풀러**(`aws-0-ap-northeast-2.pooler.supabase.com:5432`, 사용자 `postgres.<ref>`):
   ```
   postgresql+asyncpg://postgres.<ref>:<pw>@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres
   ```
   ⚠️ 직결 호스트(db.\<ref\>.supabase.co)는 IPv6 전용 — Cloud Run(IPv4)에서 안 된다.
   ⚠️ 트랜잭션 모드(6543) 금지 — asyncpg·advisory lock 이 깨진다.
3. 텔레그램 봇 토큰/챗 ID, 토스 자격증명, Anthropic 키, 강한 `API_KEY` 준비

## 적용 순서

> **2026-07-11 진행 상태**: 프로젝트 `toss-trader-kr` 생성·결제 연결·API 활성·Artifact Registry·
> 서비스계정 2·시크릿 껍데기 8종까지 **완료**(gcloud 선생성 — Terraform 상태 흡수는
> [bootstrap-import.tf](bootstrap-import.tf) 가 첫 apply 에서 자동 수행, **성공 후 그 파일 삭제**).
> 남은 것: 시크릿 값 주입(2단계) → 이미지 푸시 → terraform apply → 1단계 검증.

```powershell
cd infra
Copy-Item terraform.tfvars.example terraform.tfvars   # 값 채우기(커밋 금지 — gitignore)
terraform init
```

**시크릿 버전 주입(TF 밖 — state 에 비밀 금지, 운영자 직접 실행).** 8종 전부 없으면 Cloud Run
배포가 실패한다. NAVER 2종은 **NCP NAVER API HUB** Application 의 Client ID/Secret
(콘솔: All Services > Application Services > NAVER API HUB > Application 등록 > 뉴스 검색 선택 >
[인증 정보]). developers.naver.com 검색 API 는 NCP 로 이관됨(2026) — 한시적 무료:

```powershell
powershell -File infra\inject-secrets.ps1   # .env 에 있는 키는 자동, 나머지는 비표시 프롬프트
```

**이미지 빌드·푸시** (로컬 Docker 또는 `gcloud builds submit`):

```powershell
gcloud auth configure-docker asia-northeast3-docker.pkg.dev
docker build -t asia-northeast3-docker.pkg.dev/<PROJECT>/toss-trader/server:v1 ..\server
docker push  asia-northeast3-docker.pkg.dev/<PROJECT>/toss-trader/server:v1
```

**전체 적용** (`image` 변수를 위 태그로):

```powershell
terraform apply
```

## 배포 직후 1단계 검증 — Scheduler 활성화 전 관문 (§3.0-6 토스 IP 리스크)

잡 2개는 `scheduler_paused=true`(기본)로 **일시정지 상태로 생성**된다. 아래 통과 전 켜지 말 것.

```powershell
$URL = terraform output -raw run_url
$AUD = terraform output -raw oidc_audience
$TOK = gcloud auth print-identity-token --audiences=$AUD    # 플랫폼 IAM(1차) 통과용
$H = @{ Authorization = "Bearer $TOK"; "X-API-Key" = "<API_KEY>" }   # 앱 인증(2차)

Invoke-RestMethod "$URL/health"                              # 1) 기동
Invoke-RestMethod "$URL/api/status" -Headers $H              # 2) DB(persistence=true)·모드 확인
Invoke-RestMethod "$URL/api/holdings" -Headers $H            # 3) ★토스 인증이 클라우드 IP 에서 되는지
Invoke-RestMethod "$URL/internal/tick" -Method Post -Headers $H   # 4) 장중 수동 틱 1회 → 텔레그램 수신
```

3)이 401/403/차단이면: 토스 Open API 지원 채널에 IP 정책 문의. 고정 IP 가 필요하면
서버리스 VPC 커넥터 + Cloud NAT(+$10~/월) — **필요 확정 전 도입 금지**.

통과 후 — **잡별 분리 제어(§3.0 B안)**:
- 뉴스만 먼저(무료·거래 위험 0, 논문 수집 조기 시작): `terraform apply -var news_paused=false`
- 거래 틱+보고서까지(LLM 비용·자율운용 시작): `terraform apply -var news_paused=false -var trading_paused=false`

두 변수 기본값은 `true`(정지) — 명시적으로 `false` 를 줘야 켜진다.

## Cloud SQL 전환 (LIVE/M3 전 필수 — PLAN §3.0)

1. 장외에 `pg_dump`(Supabase) → 복원(Cloud SQL)
2. `terraform apply -var enable_cloud_sql=true` + DATABASE_URL 시크릿 새 버전(unix 소켓 형식)
3. Cloud Run 재배포(새 리비전이 latest 시크릿을 읽음)
