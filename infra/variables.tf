# M2 클라우드 인프라 변수 (PLAN §3.2)

variable "project_id" {
  type        = string
  description = "GCP 프로젝트 ID"
}

variable "region" {
  type    = string
  default = "asia-northeast3" # 서울 — 토스 API 해외 IP 차단 관행 대비(§3.0-6)
}

variable "service_name" {
  type    = string
  default = "toss-trader"
}

variable "image" {
  type        = string
  description = "배포 이미지 URI (예: asia-northeast3-docker.pkg.dev/PROJECT/toss-trader/server:TAG)"
}

variable "operator_email" {
  type        = string
  description = "운영자 구글 계정 — run.invoker (수동 /api/* 호출용)"
}

# DB 는 Supabase(§3.0 결정 — DATABASE_URL 시크릿). Cloud SQL 은 전환 대비 스텁.
variable "enable_cloud_sql" {
  type    = bool
  default = false
}

# 잡별 일시정지(§3.0 B안 — 뉴스/거래 분리 제어). 둘 다 기본 true(검증 전 정지).
# - 뉴스 수집: 무료·거래 위험 0 → 1단계 검증 후 먼저 켠다(news_paused=false).
# - 거래 틱+보고서: LLM 비용·자율운용 시작 → 준비되면 켠다(trading_paused=false).
variable "news_paused" {
  type    = bool
  default = true
}

variable "trading_paused" {
  type    = bool
  default = true
}

variable "tick_schedule" {
  type = string
  # 20분 = 페이퍼 운용 개시 시점 선택(2026-07-11). 5분 대비 LLM 비용 ¼.
  # 대가: 결정적 청산(손절) 감지 지연이 최대 20분 — 일봉 전략이라 수용 가능.
  # 성과 표본이 쌓인 뒤 */10·*/5 복귀 검토. Asia/Seoul — 15:30 초과분·휴장일은 서버가 거른다
  default = "*/20 9-15 * * 1-5"
}

variable "report_schedule" {
  type    = string
  default = "30 16 * * *" # 매일 호출 — 거래일/기생성은 서버가 스킵(§3.9)
}

variable "news_schedule" {
  type    = string
  default = "0,30 8-18 * * *" # 논문 뉴스 수집(§8.4) — 30분 간격 08–18시(야간 보도는 08:00 수집)
}

# ── 클라우드 샌드박스(합성 시세로 24/7 거래 능력 확인) ────────────────────────
# 토스 0콜·LLM 0콜. 운영과는 **DB 스키마**로 분리(DB_SCHEMA=sandbox — public 이 보이지 않음).
variable "enable_sandbox" {
  type    = bool
  default = false
}

variable "sandbox_schedule" {
  type    = string
  default = "*/10 * * * *" # 24/7 10분 간격(장시간 무시) — 틱 1회 = 시뮬 1일
}

variable "sandbox_paused" {
  type    = bool
  default = true
}

# 비밀 아님(선택) — 비우면 env 미설정
variable "telegram_chat_id" {
  type    = string
  default = ""
}

variable "toss_account_seq" {
  type    = string
  default = ""
}
