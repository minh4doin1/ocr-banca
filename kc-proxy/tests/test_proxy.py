"""
Tests cho kc-proxy — verify auth, forward, error handling.

Mock httpx để không cần Keycloak thật.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def proxy_env(monkeypatch):
    """Set env tối thiểu để app khởi động được."""
    monkeypatch.setattr("app.config.settings.keycloak_client_id", "svc")
    monkeypatch.setattr("app.config.settings.keycloak_client_secret", "secret")
    monkeypatch.setattr("app.config.settings.keycloak_internal_url", "http://kc.test:8080")
    monkeypatch.setattr("app.config.settings.keycloak_realm", "agribank")
    monkeypatch.setattr("app.config.settings.proxy_path_prefix", "/api/v1/iam-bridge")
    monkeypatch.setattr("app.config.settings.proxy_api_key", "test-key-123")
    monkeypatch.setattr("app.config.settings.audit_log_enabled", False)
    return monkeypatch


@pytest.fixture
def client(proxy_env):
    """TestClient — import sau khi env đã set."""
    from app.main import app

    return TestClient(app)


@pytest.fixture
def fake_token():
    return "fake-jwt-token"


# ── Health ──


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_missing_creds(proxy_env):
    proxy_env.setattr("app.config.settings.keycloak_client_id", "")
    proxy_env.setattr("app.config.settings.keycloak_client_secret", "")
    from app.main import app

    r = TestClient(app).get("/readyz")
    assert r.status_code == 503


def test_readyz_ok(client):
    r = client.get("/readyz")
    assert r.status_code == 200


# ── Auth ──


def test_missing_proxy_key_rejected(client):
    r = client.get("/api/v1/iam-bridge/users?username=alice")
    assert r.status_code == 401


def test_wrong_proxy_key_rejected(client):
    r = client.get(
        "/api/v1/iam-bridge/users?username=alice",
        headers={"X-Proxy-Key": "wrong"},
    )
    assert r.status_code == 401


def test_correct_proxy_key_accepted(client, fake_token):
    with patch("app.keycloak_client.httpx.request") as mock_req:
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = b'[{"id":"u1"}]'
        mock_resp.headers = {"content-type": "application/json"}
        mock_req.return_value = mock_resp

        with patch.object(
            __import__("app.keycloak_client", fromlist=["kc_client"]).kc_client,
            "_get_token",
            return_value=fake_token,
        ):
            r = client.get(
                "/api/v1/iam-bridge/users?username=alice&exact=true",
                headers={"X-Proxy-Key": "test-key-123"},
            )
    assert r.status_code == 200
    assert r.json() == [{"id": "u1"}]
    assert r.headers.get("x-request-id")


# ── Forward shape ──


def test_forward_url_is_admin_path(client, fake_token):
    """Verify request được forward tới /admin/realms/agribank/<kc_path>."""
    captured = {}

    def fake_forward(self, method, path, *, params=None, body=b"", content_type=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = b"{}"
        mock_resp.headers = {"content-type": "application/json"}
        return mock_resp

    with patch.object(
        __import__("app.keycloak_client", fromlist=["kc_client"]).kc_client,
        "_get_token",
        return_value=fake_token,
    ):
        with patch.object(
            __import__("app.keycloak_client", fromlist=["KeycloakAdminClient"]).KeycloakAdminClient,
            "forward",
            fake_forward,
        ):
            r = client.post(
                "/api/v1/iam-bridge/users",
                headers={"X-Proxy-Key": "test-key-123", "Content-Type": "application/json"},
                json={"username": "alice", "enabled": True},
            )

    assert r.status_code == 200
    assert captured["method"] == "POST"
    assert captured["path"] == "/users"
    assert captured["params"] == {}
    assert b"alice" in captured["body"]


def test_forward_preserves_query_params(client, fake_token):
    captured = {}

    def fake_forward(self, method, path, *, params=None, body=b"", content_type=None):
        captured["params"] = params
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.content = b"[]"
        mock_resp.headers = {"content-type": "application/json"}
        return mock_resp

    with patch.object(
        __import__("app.keycloak_client", fromlist=["kc_client"]).kc_client,
        "_get_token",
        return_value=fake_token,
    ):
        with patch.object(
            __import__("app.keycloak_client", fromlist=["KeycloakAdminClient"]).KeycloakAdminClient,
            "forward",
            fake_forward,
        ):
            r = client.get(
                "/api/v1/iam-bridge/clients?clientId=banca-app",
                headers={"X-Proxy-Key": "test-key-123"},
            )

    assert r.status_code == 200
    assert captured["params"] == {"clientId": "banca-app"}


# ── Error paths ──


def test_upstream_error_returns_502(client):
    from app.keycloak_client import KeycloakProxyError

    with patch.object(
        __import__("app.keycloak_client", fromlist=["KeycloakAdminClient"]).KeycloakAdminClient,
        "forward",
        side_effect=KeycloakProxyError("kc unreachable"),
    ):
        r = client.get(
            "/api/v1/iam-bridge/users",
            headers={"X-Proxy-Key": "test-key-123"},
        )
    assert r.status_code == 502
    assert "kc unreachable" in r.json()["detail"]


def test_upstream_4xx_passthrough(client, fake_token):
    """Nếu Keycloak trả 4xx/5xx, proxy phải pass-through chứ không rewrite."""
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404
    mock_resp.content = b'{"error":"not found"}'
    mock_resp.headers = {"content-type": "application/json"}

    with patch.object(
        __import__("app.keycloak_client", fromlist=["kc_client"]).kc_client,
        "_get_token",
        return_value=fake_token,
    ):
        with patch.object(
            __import__("app.keycloak_client", fromlist=["KeycloakAdminClient"]).KeycloakAdminClient,
            "forward",
            return_value=mock_resp,
        ):
            r = client.get(
                "/api/v1/iam-bridge/users/missing-id",
                headers={"X-Proxy-Key": "test-key-123"},
            )
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}