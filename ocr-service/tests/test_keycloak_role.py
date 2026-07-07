"""Tests cho gán client role Keycloak."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.keycloak_service import KeycloakClient, KeycloakError


@pytest.fixture
def kc_client(monkeypatch):
    monkeypatch.setattr(
        "app.services.keycloak_service.settings.keycloak_base_url",
        "https://kc.test",
    )
    monkeypatch.setattr(
        "app.services.keycloak_service.settings.keycloak_realm", "agribank"
    )
    monkeypatch.setattr(
        "app.services.keycloak_service.settings.keycloak_client_id", "svc"
    )
    monkeypatch.setattr(
        "app.services.keycloak_service.settings.keycloak_client_secret", "secret"
    )
    return KeycloakClient()


def test_get_client_by_client_id(kc_client):
    kc_client._request = MagicMock()
    kc_client._request.return_value.status_code = 200
    kc_client._request.return_value.json.return_value = [
        {"id": "uuid-1", "clientId": "banca-app"}
    ]

    result = kc_client.get_client_by_client_id("banca-app")
    assert result["id"] == "uuid-1"


def test_assign_client_roles(kc_client):
    kc_client._request = MagicMock()
    kc_client._request.return_value.status_code = 204

    kc_client.assign_client_roles("user-1", "uuid-1", [{"id": "r1", "name": "banca-seller"}])
    kc_client._request.assert_called_once()
    args, kwargs = kc_client._request.call_args
    assert args[0] == "POST"
    assert "/role-mappings/clients/uuid-1" in args[1]


def test_get_client_role_not_found(kc_client):
    kc_client._request = MagicMock()
    kc_client._request.return_value.status_code = 404

    assert kc_client.get_client_role("uuid-1", "missing") is None


def test_get_client_role_error(kc_client):
    kc_client._request = MagicMock()
    kc_client._request.return_value.status_code = 500
    kc_client._request.return_value.text = "err"

    with pytest.raises(KeycloakError):
        kc_client.get_client_role("uuid-1", "banca-admin")
