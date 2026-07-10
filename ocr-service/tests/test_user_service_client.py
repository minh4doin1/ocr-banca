"""
Tests cho UserServiceClient — HTTP client tới user-service.

Mock requests.Session để không cần user-service thật.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.services.user_service_client import (
    UserConflictError,
    UserNotFoundError,
    UserServiceAuthError,
    UserServiceClient,
    UserServiceError,
    UserServiceUnavailableError,
)


@pytest.fixture
def client():
    return UserServiceClient(
        base_url="http://user-service.test",
        api_key="test-token",
        timeout=10,
        roles_client_id="banca-app",
    )


def _mock_resp(status: int, body: dict | list | None = None, text: str | None = None):
    """Helper tạo mock Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.text = text or ""
    if body is not None:
        resp.json.return_value = body
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


# ── Init ──


def test_init_requires_base_url():
    with pytest.raises(UserServiceError, match="USER_SERVICE_URL"):
        UserServiceClient(base_url="", api_key="x")


def test_init_strips_trailing_slash():
    c = UserServiceClient(base_url="http://x/", api_key="")
    assert c.base_url == "http://x"


# ── HTTP helpers ──


def test_401_raises_auth_error(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(401, text="invalid token")
        with pytest.raises(UserServiceAuthError):
            client.find_user_by_username("alice")


def test_403_raises_auth_error(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(403, text="forbidden")
        with pytest.raises(UserServiceAuthError):
            client.find_user_by_username("alice")


def test_409_raises_conflict_error(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(409, text="user exists")
        with pytest.raises(UserConflictError):
            client.create_user(username="alice")


def test_404_raises_not_found_error(client):
    """assign_roles không cho phép 404 → raise UserNotFoundError."""
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(404, text="user not found")
        with pytest.raises(UserNotFoundError):
            client.assign_roles("u1", ["banca-seller"])


def test_500_raises_unavailable(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(500, text="kc unreachable")
        with pytest.raises(UserServiceUnavailableError):
            client.find_user_by_username("alice")


def test_502_raises_unavailable(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(502, text="bad gateway")
        with pytest.raises(UserServiceUnavailableError):
            client.find_user_by_username("alice")


def test_400_raises_generic_error(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(400, text="bad payload")
        with pytest.raises(UserServiceError, match="HTTP 400"):
            client.create_user(username="x")


def test_connection_error_raises_unavailable(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.side_effect = requests.ConnectionError("refused")
        with pytest.raises(UserServiceUnavailableError, match="Không kết nối"):
            client.find_user_by_username("alice")


# ── find_user_by_username ──


def test_find_user_returns_dict_when_found(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {
            "found": True,
            "user": {"id": "u1", "username": "alice"},
        })
        result = client.find_user_by_username("alice")
    assert result == {"id": "u1", "username": "alice"}


def test_find_user_returns_none_when_not_found(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"found": False, "username": "alice"})
        result = client.find_user_by_username("alice")
    assert result is None


# ── create_user ──


def test_create_user_returns_id(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(201, {"id": "new-uuid", "username": "alice"})
        user_id = client.create_user(
            username="alice",
            email="alice@x.com",
            first_name="Alice",
            last_name="Nguyen",
            password="Pass1234!",
            temporary=True,
            required_actions=["UPDATE_PASSWORD"],
            attributes={"branchId": ["1500"]},
        )
    assert user_id == "new-uuid"
    args, kwargs = mock_req.call_args
    assert args[0] == "POST"
    assert args[1] == "http://user-service.test/users"
    payload = kwargs["json"]
    assert payload["username"] == "alice"
    assert payload["password"] == "Pass1234!"
    assert payload["temporary"] is True
    assert payload["requiredActions"] == ["UPDATE_PASSWORD"]
    assert payload["attributes"] == {"branchId": ["1500"]}


def test_create_user_minimal_payload(client):
    """Không gửi field optional khi không truyền."""
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(201, {"id": "u1", "username": "x"})
        client.create_user(username="x")
    payload = mock_req.call_args.kwargs["json"]
    assert payload == {"username": "x", "enabled": True}


# ── reset_password ──


def test_reset_password(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(204)
        client.reset_password("u1", "NewPass1!", temporary=False)
    args, kwargs = mock_req.call_args
    assert args[0] == "PUT"
    assert args[1] == "http://user-service.test/users/u1/password"
    assert kwargs["json"] == {"password": "NewPass1!", "temporary": False}


# ── reset_otp ──


def test_reset_otp_returns_count(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"deleted": 2})
        deleted = client.reset_otp("u1")
    assert deleted == 2


# ── Roles ──


def test_get_user_client_roles(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {
            "roles": [{"name": "banca-seller", "id": "r1"}],
        })
        roles = client.get_user_client_roles("u1")
    assert len(roles) == 1
    assert roles[0]["name"] == "banca-seller"


def test_get_user_client_roles_uses_default_client(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"roles": []})
        client.get_user_client_roles("u1")
    args, kwargs = mock_req.call_args
    assert kwargs["params"] == {"clientId": "banca-app"}


def test_get_user_client_roles_custom_client(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"roles": []})
        client.get_user_client_roles("u1", client_id="other-app")
    assert mock_req.call_args.kwargs["params"] == {"clientId": "other-app"}


def test_assign_roles_empty_returns_empty(client):
    result = client.assign_roles("u1", [])
    assert result == {"assigned": [], "skipped": []}
    # Không gọi HTTP
    with patch.object(client._session, "request") as mock_req:
        client.assign_roles("u1", [])
        mock_req.assert_not_called()


def test_assign_roles_returns_result(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {
            "assigned": ["banca-admin"],
            "skipped": ["banca-seller"],
        })
        result = client.assign_roles("u1", ["banca-seller", "banca-admin"])
    assert result == {"assigned": ["banca-admin"], "skipped": ["banca-seller"]}


def test_remove_roles_returns_result(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {
            "removed": ["banca-seller"],
            "skipped": [],
        })
        result = client.remove_roles("u1", ["banca-seller"])
    assert result == {"removed": ["banca-seller"], "skipped": []}


# ── Required actions ──


def test_ensure_required_actions(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {
            "requiredActions": ["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
        })
        result = client.ensure_required_actions("u1", ["CONFIGURE_TOTP"])
    assert "CONFIGURE_TOTP" in result
    args, _ = mock_req.call_args
    assert args[0] == "POST"
    assert args[1].endswith("/users/u1/required-actions")


def test_set_required_actions(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"requiredActions": ["X"]})
        result = client.set_required_actions("u1", ["X"])
    assert result == ["X"]
    args, _ = mock_req.call_args
    assert args[0] == "PUT"


# ── Attributes ──


def test_update_user_attributes_skips_empty(client):
    client.update_user_attributes("u1", {})
    # Không gọi HTTP
    with patch.object(client._session, "request") as mock_req:
        client.update_user_attributes("u1", {})
        mock_req.assert_not_called()


def test_update_user_attributes(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(204)
        client.update_user_attributes("u1", {"branchId": ["1500"]})
    args, _ = mock_req.call_args
    assert args[0] == "PUT"
    assert args[1].endswith("/users/u1/attributes")


# ── update_user_details ──


def test_update_user_details_skips_empty(client):
    with patch.object(client._session, "request") as mock_req:
        client.update_user_details("u1")  # nothing to update
        mock_req.assert_not_called()


def test_update_user_details(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(204)
        client.update_user_details(
            "u1", email="a@b.com", first_name="A", last_name="Nguyen"
        )
    payload = mock_req.call_args.kwargs["json"]
    assert payload == {
        "email": "a@b.com",
        "firstName": "A",
        "lastName": "Nguyen",
    }


# ── Auth header ──


def test_sends_x_service_token(client):
    with patch.object(client._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"found": False, "username": "x"})
        client.find_user_by_username("x")
    headers = mock_req.call_args.kwargs["headers"]
    assert headers["X-Service-Token"] == "test-token"


def test_no_token_when_api_key_empty():
    c = UserServiceClient(base_url="http://x", api_key="")
    with patch.object(c._session, "request") as mock_req:
        mock_req.return_value = _mock_resp(200, {"found": False, "username": "x"})
        c.find_user_by_username("x")
    headers = mock_req.call_args.kwargs["headers"]
    assert "X-Service-Token" not in headers


# ── Health ──


def test_health_true_when_ok(client):
    with patch.object(client._session, "get") as mock_get:
        mock_get.return_value = _mock_resp(200, body={"status": "ok"})
        assert client.health() is True


def test_health_false_when_down(client):
    with patch.object(client._session, "get") as mock_get:
        mock_get.side_effect = requests.ConnectionError("refused")
        assert client.health() is False