# M2 클라우드 자율 운용 인프라 (PLAN §3.2)
# 적용 순서는 infra/README.md — 시크릿 버전 주입(TF 밖) 없이는 Cloud Run 배포가 실패한다.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # OIDC audience — Cloud Run URL 은 생성 후에야 확정되므로(닭-달걀) 커스텀 audience 로 고정.
  # Cloud Run custom_audiences · Scheduler oidc_token.audience · 앱 OIDC_AUDIENCE 세 곳이 일치해야 한다.
  audience = "https://${var.service_name}-${var.project_id}"

  # 값은 TF 밖에서 주입(`gcloud secrets versions add …`) — state 에 비밀 금지(§3.2)
  secret_names = [
    "TOSS_CLIENT_ID",
    "TOSS_CLIENT_SECRET",
    "ANTHROPIC_API_KEY",
    "API_KEY",
    "DATABASE_URL", # Supabase 세션 풀러(§3.0) — Cloud SQL 전환 시 새 버전으로 교체
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NAVER_CLIENT_ID", # 논문 뉴스 수집(§8) — 네이버 검색 API
    "NAVER_CLIENT_SECRET",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "sqladmin.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# ── 이미지 저장소 ──────────────────────────────────────────────────────────────
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# ── 시크릿(껍데기만 — 버전은 gcloud 로) ────────────────────────────────────────
resource "google_secret_manager_secret" "s" {
  for_each  = toset(local.secret_names)
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# ── 서비스 계정(최소 권한) ─────────────────────────────────────────────────────
resource "google_service_account" "run_sa" {
  account_id   = "${var.service_name}-run"
  display_name = "Cloud Run 실행 SA (Secret accessor)"
}

resource "google_service_account" "scheduler_sa" {
  account_id   = "${var.service_name}-sched"
  display_name = "Cloud Scheduler 호출 SA (run.invoker)"
}

resource "google_secret_manager_secret_iam_member" "run_secret_access" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run_sa.email}"
}

# ── Cloud Run (request-based: min=0 — 요청 중에만 과금) ───────────────────────
resource "google_cloud_run_v2_service" "svc" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # 플랫폼 IAM 이 1차 방벽(아래 invoker 멤버만 — allUsers 없음)

  custom_audiences = [local.audience]

  template {
    service_account = google_service_account.run_sa.email
    timeout         = "900s" # 틱이 수 분(LLM 직렬 호출) — §3.0-4

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # §3.4 advisory lock 과 함께 이중 직렬화
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "APP_ENV" # §3.7 하드닝(docs 차단·기본 API키 기동 거부)
        value = "production"
      }
      env {
        name  = "TICK_INTERVAL_SEC" # 내장 루프 OFF — 틱은 Scheduler 가 호출(§3.0-1)
        value = "0"
      }
      env {
        name  = "OIDC_AUDIENCE"
        value = local.audience
      }
      env {
        name  = "SCHEDULER_SA_EMAIL"
        value = google_service_account.scheduler_sa.email
      }
      env {
        name  = "NEWS_TARGETS_PATH" # 논문 뉴스 수집 유니버스(§8.4 — 이미지에 번들, git 추적)
        value = "/app/data/news_targets.json"
      }

      dynamic "env" {
        for_each = var.telegram_chat_id == "" ? [] : [var.telegram_chat_id]
        content {
          name  = "NOTIFY_TELEGRAM_CHAT_ID"
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.toss_account_seq == "" ? [] : [var.toss_account_seq]
        content {
          name  = "TOSS_ACCOUNT_SEQ"
          value = env.value
        }
      }

      dynamic "env" {
        for_each = toset(local.secret_names)
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.run_secret_access]
}

# invoker = scheduler-sa + 운영자만(--no-allow-unauthenticated 상당)
resource "google_cloud_run_v2_service_iam_member" "invoker_scheduler" {
  name     = google_cloud_run_v2_service.svc.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "invoker_operator" {
  name     = google_cloud_run_v2_service.svc.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "user:${var.operator_email}"
}

# ── Cloud Scheduler 잡 2개 (Asia/Seoul — UTC 환산 불필요, §3.2) ────────────────
resource "google_cloud_scheduler_job" "tick" {
  name             = "${var.service_name}-tick"
  region           = var.region
  schedule         = var.tick_schedule
  time_zone        = "Asia/Seoul"
  attempt_deadline = "900s"             # 기본 3분이면 틱 도중 잘림(§3.0-4)
  paused           = var.trading_paused # 거래 틱 — LLM 비용·자율운용 시작점(뉴스와 분리 제어)

  retry_config {
    retry_count = 0 # 다음 파이어가 커버 — 재시도는 중복만 만든다(락이 직렬화하지만 무의미)
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.svc.uri}/internal/tick"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = local.audience
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "report" {
  name             = "${var.service_name}-report"
  region           = var.region
  schedule         = var.report_schedule
  time_zone        = "Asia/Seoul"
  attempt_deadline = "300s"
  paused           = var.trading_paused # 보고서는 페이퍼 운용 산출물 — 거래와 함께 켠다

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.svc.uri}/internal/report?force=false"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = local.audience
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_scheduler_job" "news" {
  name             = "${var.service_name}-news"
  region           = var.region
  schedule         = var.news_schedule
  time_zone        = "Asia/Seoul"
  attempt_deadline = "600s"          # 200종목 × ~0.2s + 삽입 — 여유
  paused           = var.news_paused # 논문 뉴스 수집 — 무료·거래 위험 0이라 먼저 켤 수 있음

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.svc.uri}/internal/news/collect"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = local.audience
    }
  }

  depends_on = [google_project_service.apis]
}

# ── 클라우드 샌드박스: 합성 시세로 24/7 거래 능력 확인 (enable_sandbox) ────────
# 운영 서비스와 분리된 Cloud Run + 전용 Scheduler. 토스·Anthropic 시크릿을 **주지 않는다**
# (합성 시세·결정적 폴백 판단 → 외부 호출 0·비용 0). DB 는 같은 Supabase 의 sandbox 스키마.
resource "google_cloud_run_v2_service" "sandbox" {
  count    = var.enable_sandbox ? 1 : 0
  name     = "${var.service_name}-sandbox"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  custom_audiences = [local.audience]

  template {
    service_account = google_service_account.run_sa.email
    timeout         = "900s"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "APP_ENV"
        value = "production"
      }
      env {
        name  = "SANDBOX_MODE"
        value = "true"
      }
      env {
        name  = "DB_SCHEMA" # ★ 운영(public)과 구조적 분리 — 없으면 앱이 기동을 거부한다
        value = "sandbox"
      }
      env {
        name  = "SANDBOX_DAY_SECONDS" # 틱 간격과 맞춤(틱 1회 = 시뮬 1일)
        value = "600"
      }
      env {
        name  = "ENFORCE_MARKET_HOURS" # 24/7 관측
        value = "false"
      }
      env {
        name  = "TICK_INTERVAL_SEC" # 내장 루프 OFF — Scheduler 가 구동
        value = "0"
      }
      env {
        name  = "OIDC_AUDIENCE"
        value = local.audience
      }
      env {
        name  = "SCHEDULER_SA_EMAIL"
        value = google_service_account.scheduler_sa.email
      }
      env {
        name  = "SYMBOL_SOURCE_PATH"
        value = "/app/data/krx_symbols.json"
      }

      # 필요한 시크릿은 API_KEY·DATABASE_URL 뿐(토스·Anthropic·텔레그램 미주입 = 외부 호출 0)
      dynamic "env" {
        for_each = toset(["API_KEY", "DATABASE_URL"])
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.run_secret_access]
}

resource "google_cloud_run_v2_service_iam_member" "sandbox_invoker_scheduler" {
  count    = var.enable_sandbox ? 1 : 0
  name     = google_cloud_run_v2_service.sandbox[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

resource "google_cloud_run_v2_service_iam_member" "sandbox_invoker_operator" {
  count    = var.enable_sandbox ? 1 : 0
  name     = google_cloud_run_v2_service.sandbox[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "user:${var.operator_email}"
}

resource "google_cloud_scheduler_job" "sandbox_tick" {
  count            = var.enable_sandbox ? 1 : 0
  name             = "${var.service_name}-sandbox-tick"
  region           = var.region
  schedule         = var.sandbox_schedule
  time_zone        = "Asia/Seoul"
  attempt_deadline = "900s"
  paused           = var.sandbox_paused

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.sandbox[0].uri}/internal/tick"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
      audience              = local.audience
    }
  }

  depends_on = [google_project_service.apis]
}

# ── Cloud SQL 스텁 (§3.0 DB 결정: 기본 미생성 — Supabase→전환 시 변수 토글) ─────
resource "google_sql_database_instance" "pg" {
  count               = var.enable_cloud_sql ? 1 : 0
  name                = "${var.service_name}-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = true

  settings {
    tier      = "db-f1-micro"
    disk_size = 10
    disk_type = "PD_SSD"
    backup_configuration {
      enabled = true
    }
  }
}
