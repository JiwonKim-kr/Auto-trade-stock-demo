output "run_url" {
  value       = google_cloud_run_v2_service.svc.uri
  description = "Cloud Run 서비스 URL — 1단계 검증(§3.2)과 수동 호출에 사용"
}

output "oidc_audience" {
  value       = local.audience
  description = "수동 호출 시 identity token 의 audience (gcloud auth print-identity-token --audiences=…)"
}

output "scheduler_sa_email" {
  value = google_service_account.scheduler_sa.email
}

output "sandbox_url" {
  value       = var.enable_sandbox ? google_cloud_run_v2_service.sandbox[0].uri : null
  description = "샌드박스 Cloud Run URL(enable_sandbox=true 일 때) — 합성 시세 24/7 거래 확인용"
}

output "artifact_repo" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}"
  description = "docker push 대상 (이미지: {repo}/server:TAG)"
}
