"""
Tests cho tính năng tạo lô user qua user-service (Node.js BE).

Sau giai đoạn 3: thay vì gọi KeycloakClient trực tiếp, router gọi
UserServiceClient. Tests mock UserServiceClient (HTTP layer).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models.schemas import (
    CellData,
    KeycloakUserInput,
    OcrResult,
    OnConflictAction,
    PageResult,
    ProvisionStatus,
    TableData,
)
from app.routers import users as users_router
from app.routers.users import _provision_one_via_user_service
from app.services.keycloak_env import resolve_keycloak_profile
from app.services.user_mapping import map_result_to_users, normalize_role, normalize_roles
from app.services.user_service_client import UserServiceError

_KC_DEV = resolve_keycloak_profile("dev")


@pytest.fixture(autouse=True)
def _roles_client_configured(monkeypatch):
    """Đảm bảo keycloak_roles_client_id đã set + clear cache."""
    monkeypatch.setattr(
        "app.routers.users.settings.keycloak_roles_client_id", "banca-app"
    )
    users_router._client_uuid_cache.clear()


# ── Mock factory cho UserServiceClient ──


def _client_new_user() -> MagicMock:
    """Mock UserServiceClient với user chưa tồn tại (tạo mới)."""
    client = MagicMock()
    client.find_user_by_username.return_value = None
    client.create_user.return_value = "new-id-123"
    client.get_user_client_roles.return_value = []
    client.assign_roles.return_value = {"assigned": ["banca-seller"], "skipped": []}
    client.remove_roles.return_value = {"removed": [], "skipped": []}
    return client


def _client_existing_user(
    existing_roles: list[dict] | None = None,
) -> MagicMock:
    """Mock UserServiceClient với user đã tồn tại (update)."""
    client = MagicMock()
    client.find_user_by_username.return_value = {
        "id": "existing-id",
        "username": "u@agribank.com.vn",
    }
    client.reset_otp.return_value = 1
    client.get_user_client_roles.return_value = existing_roles or []
    client.assign_roles.return_value = {"assigned": [], "skipped": []}
    client.remove_roles.return_value = {"removed": [], "skipped": []}
    return client


def _user(**kwargs) -> KeycloakUserInput:
    data = {
        "username": "u@agribank.com.vn",
        "email": "u@agribank.com.vn",
        "first_name": "A",
        "last_name": "Nguyễn",
        "branch_code": "1500",
        "ipcas_code": "HQPTEST",
        "cccd": "001234567890",
        "phone": "0982867163",
        "unit_code": "95204001",
        "role": "banca-seller",
    }
    data.update(kwargs)
    return KeycloakUserInput(**data)


# ── Normalize role (giữ nguyên) ──


def test_normalize_role_vn_label():
    assert normalize_role("Phê duyệt viên") == "banca-accounting-controller"
    assert normalize_role("banca-seller") == "banca-seller"
    assert normalize_role("Phê duyệt viên Đại lý viên") == "banca-accounting-controller"
    assert normalize_role("Kê toán viên Đại lý viên") == "banca-accounting-operator"
    assert normalize_role("Quản trị; Đại lý VIÊN") == "banca-admin"


def test_normalize_roles_multi_without_delimiter():
    roles = normalize_roles("Phe duyet vien Dai ly vien")
    assert "banca-accounting-controller" in roles
    assert "banca-seller" in roles
    assert len(roles) == 2


def test_normalize_roles_ocr_variants():
    assert normalize_roles("dai li") == ["banca-seller"]
    assert normalize_roles("KT vien") == ["banca-accounting-operator"]
    assert normalize_roles("Quan tri; Ke toan vien") == [
        "banca-admin",
        "banca-accounting-operator",
    ]


# ── Provision qua user-service ──


def test_provision_creates_new_user():
    client = _client_new_user()
    user = _user(password="Pass@123")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
    )
    assert result.status == ProvisionStatus.CREATED
    client.create_user.assert_called_once()
    client.assign_roles.assert_called_once()
    kwargs = client.create_user.call_args.kwargs
    assert kwargs["username"] == "u@agribank.com.vn"
    assert kwargs["required_actions"] == ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]
    # Role được truyền qua user-service dưới dạng tên (string), không phải UUID dict
    assign_args, _ = client.assign_roles.call_args
    assert assign_args[1] == ["banca-seller"]


def test_provision_existing_user_save_details_and_role():
    client = _client_existing_user()
    user = _user()
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.create_user.assert_not_called()
    client.update_user_details.assert_called_once()
    client.assign_roles.assert_called_once()
    client.reset_password.assert_not_called()


def test_provision_reset_password():
    client = _client_existing_user()
    user = _user(password="New@123")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.RESET_PASSWORD,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_password.assert_called_once()
    args, kwargs = client.reset_password.call_args
    assert args[0] == "existing-id"
    pwd = args[1] if len(args) > 1 else kwargs.get("password", "")
    assert pwd.startswith("Ngay") and pwd.endswith("@")
    assert kwargs.get("temporary", args[2] if len(args) > 2 else None) is True


def test_provision_reset_both_keeps_update_password():
    """RESET_BOTH: reset OTP không được xóa mất yêu cầu đổi mật khẩu."""
    client = _client_existing_user()
    user = _user()
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.RESET_BOTH,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_password.assert_called_once()
    client.reset_otp.assert_called_once()
    client.ensure_required_actions.assert_called_once_with(
        "existing-id", ["UPDATE_PASSWORD"]
    )
    # ensure_required_actions phải chạy SAU reset_otp (vì reset_otp ghi đè list).
    method_order = [
        c[0]
        for c in client.mock_calls
        if c[0] in ("reset_otp", "ensure_required_actions")
    ]
    assert method_order == ["reset_otp", "ensure_required_actions"]
    assert "reset_password" in result.actions_applied
    assert "require_action:UPDATE_PASSWORD" in result.actions_applied


def test_provision_reset_password_requires_update_action():
    """RESET_PASSWORD: gán required action UPDATE_PASSWORD để ép đổi sau đăng nhập."""
    client = _client_existing_user()
    user = _user()
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.RESET_PASSWORD,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.ensure_required_actions.assert_called_once_with(
        "existing-id", ["UPDATE_PASSWORD"]
    )
    _args, kwargs = client.reset_password.call_args
    assert kwargs.get("temporary") is True
    assert "require_action:UPDATE_PASSWORD" in result.actions_applied


def test_provision_existing_user_removes_unselected_role():
    """User đã có 2 role trên Keycloak, bỏ 1 role thì phải gỡ role đó."""
    client = _client_existing_user(
        existing_roles=[
            {"name": "banca-seller", "id": "id-seller"},
            {"name": "banca-accounting-operator", "id": "id-acc"},
        ]
    )
    user = _user(roles=["banca-seller"], role="banca-seller")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    # remove_roles được gọi với tên role (string), không phải UUID dict
    client.remove_roles.assert_called_once()
    args, _ = client.remove_roles.call_args
    assert "banca-accounting-operator" in args[1]
    assert "remove_role:banca-accounting-operator" in result.actions_applied
    assert "role_already:banca-seller" in result.actions_applied


def test_provision_user_service_auth_error_surfaces():
    """Khi user-service trả 401/403, provision fail với message rõ."""
    from app.services.user_service_client import UserServiceAuthError

    client = _client_new_user()
    client.create_user.side_effect = UserServiceAuthError("X-Service-Token invalid")
    user = _user(password="Pass@123")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.FAILED
    assert "user-service" in result.error or "unavailable" in result.error


def test_provision_user_service_conflict_maps_409():
    """Khi user-service trả 409 (race condition), provision fail với error user_exists."""
    from app.services.user_service_client import UserConflictError

    client = _client_new_user()
    client.create_user.side_effect = UserConflictError("User exists")
    user = _user(password="Pass@123")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.FAILED
    assert "User exists" in result.error


def test_provision_fails_without_role():
    client = _client_new_user()
    user = _user(role="")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.FAILED
    assert "role" in result.error


def test_provision_generic_user_service_error():
    """Các lỗi chung từ user-service được surface vào result.error."""
    client = _client_new_user()
    client.create_user.side_effect = UserServiceError("upstream 400: bad payload")
    user = _user(password="Pass@123")
    result = _provision_one_via_user_service(
        client,
        user,
        kc=_KC_DEV,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.FAILED
    assert "bad payload" in result.error