"""/internal/* OIDC 인증(§3.3) — Scheduler Bearer 경로 + API 키 폴백.

verify_oauth2_token 은 목으로 대체(실제 구글 공개키 조회 없음). 검증 대상:
성공 / 서비스 계정 불일치 / 검증 예외 → 전부 401(fail-closed) / Bearer 제시 시
API 키 폴백 차단 / 미설정(로컬)은 기존 API 키 경로 그대로.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.core.settings import get_settings
from app.main import create_app

SA = "scheduler@proj.iam.gserviceaccount.com"
KEY = {"X-API-Key": "dev-local-key"}
BEARER = {"Authorization": "Bearer dummy-token"}


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("OIDC_AUDIENCE", "https://svc.run.app")
    monkeypatch.setenv("SCHEDULER_SA_EMAIL", SA)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _client():
    app = create_app()
    return TestClient(app)


# /internal/report 를 대상으로 검증(DB 없음 → {"skipped": …} 200 — 인증 계층만 관찰)
def test_oidc_bearer_accepted(oidc_env):
    oidc_env.setattr(deps.id_token, "verify_oauth2_token",
                     lambda t, r, aud: {"email": SA, "email_verified": True, "aud": aud})
    with _client() as c:
        r = c.post("/internal/report", headers=BEARER)
        assert r.status_code == 200 and "skipped" in r.json()


def test_oidc_wrong_service_account_401(oidc_env):
    oidc_env.setattr(deps.id_token, "verify_oauth2_token",
                     lambda t, r, aud: {"email": "evil@x.com", "email_verified": True})
    with _client() as c:
        assert c.post("/internal/report", headers=BEARER).status_code == 401


def test_oidc_verify_error_becomes_401_not_500(oidc_env):
    def boom(t, r, aud):
        raise ValueError("만료/위조/공개키 조회 실패")
    oidc_env.setattr(deps.id_token, "verify_oauth2_token", boom)
    with _client() as c:
        assert c.post("/internal/report", headers=BEARER).status_code == 401


def test_bearer_does_not_fall_back_to_api_key(oidc_env):
    # 잘못된 Bearer + 유효한 API 키 동시 제시 → 401 (잘못된 토큰의 조용한 통과 방지)
    def boom(t, r, aud):
        raise ValueError("invalid")
    oidc_env.setattr(deps.id_token, "verify_oauth2_token", boom)
    with _client() as c:
        assert c.post("/internal/report", headers={**BEARER, **KEY}).status_code == 401


def test_api_key_path_preserved_without_oidc_config():
    # OIDC 미설정(로컬 기본) — Bearer 가 있어도 audience 없으면 API 키 경로로
    with _client() as c:
        assert c.post("/internal/report", headers=KEY).status_code == 200
        assert c.post("/internal/report", headers=BEARER).status_code == 401
        assert c.post("/internal/report").status_code == 401
