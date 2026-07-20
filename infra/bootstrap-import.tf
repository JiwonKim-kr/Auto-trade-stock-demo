# gcloud 로 선생성된 리소스(2026-07-11 — terraform apply 가 자동화 정책으로 차단되어 개별
# gcloud 명령으로 생성)를 Terraform 상태로 흡수하는 config-driven import 블록(TF 1.7+).
# ⚠️ 첫 terraform apply 성공 후 이 파일은 삭제한다(이미 상태에 있는 리소스 재-import 는 오류).
# google_project_service 는 import 불필요 — enable 은 멱등이라 apply 가 그대로 흡수한다.

import {
  to = google_artifact_registry_repository.repo
  id = "projects/${var.project_id}/locations/${var.region}/repositories/${var.service_name}"
}

import {
  to = google_service_account.run_sa
  id = "projects/${var.project_id}/serviceAccounts/${var.service_name}-run@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = google_service_account.scheduler_sa
  id = "projects/${var.project_id}/serviceAccounts/${var.service_name}-sched@${var.project_id}.iam.gserviceaccount.com"
}

import {
  for_each = toset(local.secret_names)
  to       = google_secret_manager_secret.s[each.value]
  id       = "projects/${var.project_id}/secrets/${each.value}"
}

import {
  for_each = toset(local.secret_names)
  to       = google_secret_manager_secret_iam_member.run_secret_access[each.value]
  id       = "projects/${var.project_id}/secrets/${each.value} roles/secretmanager.secretAccessor serviceAccount:${var.service_name}-run@${var.project_id}.iam.gserviceaccount.com"
}
