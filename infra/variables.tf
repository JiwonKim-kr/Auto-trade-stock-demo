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

# 배포 직후 1단계 검증(토스 IP — §3.2) 전까지 잡을 일시정지 상태로 생성한다.
# 검증 통과 후 false 로 바꿔 apply(또는 콘솔에서 resume).
variable "scheduler_paused" {
  type    = bool
  default = true
}

variable "tick_schedule" {
  type    = string
  default = "*/5 9-15 * * 1-5" # Asia/Seoul — 15:30 초과분·휴장일은 서버가 거른다
}

variable "report_schedule" {
  type    = string
  default = "30 16 * * *" # 매일 호출 — 거래일/기생성은 서버가 스킵(§3.9)
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
