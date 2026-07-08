"""
Tests cho tính năng tạo lô user Keycloak.

- Map kết quả OCR -> danh sách KeycloakUserInput.
- Các nhánh on_conflict trong _provision_one (mock KeycloakClient).
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
from app.routers.users import _provision_one
from app.services.user_mapping import map_result_to_users, normalize_role, normalize_roles


@pytest.fixture(autouse=True)
def _roles_client_configured(monkeypatch):
    monkeypatch.setattr(
        "app.routers.users.settings.keycloak_roles_client_id", "banca-app"
    )
    users_router._client_uuid_cache.clear()


def _make_table(header: list[str], rows: list[list[str]]) -> TableData:
    matrix = [header] + rows
    num_rows = len(matrix)
    num_cols = max(len(r) for r in matrix)
    cells: list[CellData] = []
    for r, row in enumerate(matrix):
        for c in range(num_cols):
            text = row[c] if c < len(row) else ""
            cells.append(CellData(row=r, col=c, text=text, confidence=1.0))
    return TableData(
        table_index=0, num_rows=num_rows, num_cols=num_cols, cells=cells
    )


def _make_result(table: TableData) -> OcrResult:
    return OcrResult(
        job_id="job1",
        filename="f.pdf",
        total_pages=1,
        pages=[PageResult(page_number=1, tables=[table])],
    )


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
    assert normalize_roles("garbage xyz") == []


def test_map_result_basic():
    table = _make_table(
        ["Email", "Họ", "Tên", "CCCD", "Mã chi nhánh", "Mã IPCAS", "Số điện thoại", "Mã đơn vị", "Vai trò"],
        [
            [
                "a@example.com",
                "Nguyễn",
                "Văn A",
                "001234567890",
                "1500",
                "HQPTEST",
                "0982867163",
                "95204001",
                "Đại lý viên",
            ],
        ],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert warnings == []
    assert users[0].username == "a@example.com"
    assert users[0].role == "banca-seller"
    assert users[0].ipcas_code == "HQPTEST"


def test_map_sso_data_without_header_row():
    table = _make_table(
        [
            "1",
            "Nguyễn Thị Phú Lương",
            "KT&NQ",
            "LANLUONG",
            "054178007182 (10/08/2021)",
            "luongnguyenthiphu@agribank.com.vn",
            "0908976096",
            "Phê duyệt viên",
            "Cấp mới",
        ],
        [],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert not warnings or not any("email" in w for w in warnings)
    assert len(users) == 1
    assert users[0].username == "luongnguyenthiphu@agribank.com.vn"
    assert users[0].ipcas_code == "LANLUONG"
    assert users[0].cccd == "054178007182"
    assert users[0].phone == "0908976096"
    assert users[0].role == "banca-accounting-controller"
    assert users[0].first_name == "Lương"
    assert users[0].last_name == "Nguyễn Thị Phú"
    assert users[0].notes == "Cấp mới"


def test_map_sso_standard_form_with_unit_code():
    """Form chuẩn: Phòng/Đơn vị lặp + Ghi chú là mã ĐV."""
    from app.services.user_mapping import _parse_department_cell, _parse_unit_or_notes

    assert _parse_department_cell("6900 Hội sở") == (
        "6900 Hội sở",
        "6900",
        "Hội sở",
    )
    assert _parse_unit_or_notes("82204001") == ("82204001", "")
    assert _parse_unit_or_notes("Cấp mới") == ("", "Cấp mới")

    table = _make_table(
        [
            "1",
            "Lê Mai Phương",
            "6900 Hội sở",
            "TGILMP",
            "081234567890",
            "phuongle@agribank.com.vn",
            "0907809680",
            "Quản trị",
            "82204001",
        ],
        [],
    )
    users, _ = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert users[0].branch_code == "6900"
    assert users[0].branch_name == "Hội sở"
    assert users[0].department_name == "6900 Hội sở"
    assert users[0].unit_code == "82204001"


def test_map_sso_fallback_email_from_ipcas_when_email_blank():
    table = _make_table(
        [
            "1",
            "Lê Mai Phương",
            "6900 Hội sở",
            "TGILMP",
            "082180011286",
            "",
            "0907809680",
            "Quản trị",
            "82204001",
        ],
        [],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert warnings == []
    assert len(users) == 1
    assert users[0].email == "tgilmp@agribank.com.vn"
    assert users[0].username == "tgilmp@agribank.com.vn"
    assert users[0].ipcas_code == "TGILMP"


def test_map_sso_not_derive_email_from_domain_fragment_seed():
    table = _make_table(
        [
            "1",
            "User Test",
            "6900 Hội sở",
            "bank.com.vn",
            "082180011286",
            "",
            "0907809680",
            "Quản trị",
            "82204001",
        ],
        [],
    )
    users, _ = map_result_to_users(_make_result(table))
    assert len(users) == 0


def _client_new_user() -> MagicMock:
    client = MagicMock()
    client.find_user_by_username.return_value = None
    client.create_user.return_value = "new-id-123"
    client.get_client_by_client_id.return_value = {"id": "client-uuid"}
    client.get_client_role.return_value = {"id": "role-id", "name": "banca-seller"}
    client.get_user_client_roles.return_value = []
    client.get_user_client_roles_optional.return_value = ([], None)
    client.assign_client_roles_batch = client.assign_client_roles
    return client


def _client_existing_user() -> MagicMock:
    client = MagicMock()
    client.find_user_by_username.return_value = {"id": "existing-id", "username": "u"}
    client.reset_otp.return_value = 1
    client.get_client_by_client_id.return_value = {"id": "client-uuid"}
    client.get_client_role.return_value = {"id": "role-id", "name": "banca-seller"}
    client.get_user_client_roles.return_value = []
    client.get_user_client_roles_optional.return_value = ([], None)
    client.assign_client_roles_batch = client.assign_client_roles
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


def test_provision_creates_new_user():
    client = _client_new_user()
    user = _user(password="Pass@123")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=["UPDATE_PASSWORD", "CONFIGURE_TOTP"],
    )
    assert result.status == ProvisionStatus.CREATED
    client.create_user.assert_called_once()
    assert client.assign_client_roles.called or client.assign_client_roles_batch.called
    kwargs = client.create_user.call_args.kwargs
    assert kwargs["username"] == "u@agribank.com.vn"
    assert kwargs["required_actions"] == ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]


def test_provision_existing_user_save_details_and_role():
    client = _client_existing_user()
    user = _user()
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.create_user.assert_not_called()
    client.update_user_details.assert_called_once()
    assert client.assign_client_roles.called or client.assign_client_roles_batch.called
    client.reset_password.assert_not_called()


def test_provision_reset_password():
    client = _client_existing_user()
    user = _user(password="New@123")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.RESET_PASSWORD,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_password.assert_called_once()
    args, kwargs = client.reset_password.call_args
    assert args[0] == "existing-id"
    pwd = args[1] if len(args) > 1 else kwargs.get("value", "")
    assert pwd.startswith("Ngay") and pwd.endswith("@")
    assert kwargs.get("temporary", args[2] if len(args) > 2 else None) is True


def test_provision_reset_both_keeps_update_password():
    """RESET_BOTH: reset OTP không được xóa mất yêu cầu đổi mật khẩu."""
    client = _client_existing_user()
    user = _user()
    result = _provision_one(
        client,
        user,
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
        c[0] for c in client.mock_calls
        if c[0] in ("reset_otp", "ensure_required_actions")
    ]
    assert method_order == ["reset_otp", "ensure_required_actions"]
    assert "reset_password" in result.actions_applied
    assert "require_action:UPDATE_PASSWORD" in result.actions_applied


def test_provision_reset_password_requires_update_action():
    """RESET_PASSWORD: gán required action UPDATE_PASSWORD để ép đổi sau đăng nhập."""
    client = _client_existing_user()
    user = _user()
    result = _provision_one(
        client,
        user,
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
    client = _client_existing_user()
    client.get_user_client_roles_optional.return_value = (
        [
            {"name": "banca-seller", "id": "id-seller"},
            {"name": "banca-accounting-operator", "id": "id-acc"},
        ],
        None,
    )
    client.get_client_role.side_effect = (
        lambda client_uuid, name: {"id": f"id-{name}", "name": name}
    )
    user = _user(roles=["banca-seller"], role="banca-seller")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.remove_client_roles_batch.assert_called_once()
    args, kwargs = client.remove_client_roles_batch.call_args
    removed = args[2] if len(args) > 2 else kwargs["roles"]
    removed_names = {p["name"] for p in removed}
    assert removed_names == {"banca-accounting-operator"}
    assert "remove_role:banca-accounting-operator" in result.actions_applied
    assert "role_already:banca-seller" in result.actions_applied


def test_provision_existing_user_keeps_roles_when_lookup_forbidden():
    """Khi không đọc được role hiện có (403) thì không gỡ role để tránh xóa nhầm."""
    client = _client_existing_user()
    client.get_user_client_roles_optional.return_value = ([], "403")
    client.get_client_role.side_effect = (
        lambda client_uuid, name: {"id": f"id-{name}", "name": name}
    )
    user = _user(roles=["banca-seller"], role="banca-seller")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.remove_client_roles_batch.assert_not_called()


def test_provision_fails_without_role():
    client = _client_new_user()
    user = _user(role="")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.FAILED
    assert "role" in result.error


def test_provision_created_when_role_assignment_forbidden():
    client = _client_new_user()

    def _assign_403(*_a, **_k):
        raise users_router.KeycloakError(
            "Gán client role (user x) thất bại (HTTP 403): forbidden"
        )

    client.assign_client_roles_batch.side_effect = _assign_403
    client.assign_client_roles.side_effect = _assign_403
    user = _user(password="Pass@123")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.CREATED
    assert "assign_role_skipped:banca-seller" in result.actions_applied
    assert "roles_assignment_failed:403" in result.actions_applied


def test_build_keycloak_attributes_sop_fields():
    from app.services.user_mapping import build_keycloak_attributes

    user = _user()
    attrs = build_keycloak_attributes(user)
    assert attrs["ipcasCode"] == ["HQPTEST"]
    assert attrs["phoneNumber"] == ["0982867163"]
    assert attrs["unitCode"] == ["95204001"]
    assert attrs["branchId"] == ["1500"]
    assert attrs["phone"] == ["0982867163"]
    assert attrs["idNo"] == ["001234567890"]
    assert attrs["branchCode"] == ["1500"]

def test_branch_code_digits_only():
    from app.services.user_mapping import _parse_branch_code_digits, _parse_department_cell

    assert _parse_branch_code_digits("6900") == "6900"
    assert _parse_branch_code_digits("6900.0") == "6900"
    assert _parse_branch_code_digits("6900 Hội sở") == "6900"
    full, code, name = _parse_department_cell("6900")
    assert code == "6900"
    assert name == ""


def test_map_sso_branch_from_digits_only_department():
    table = _make_table(
        [
            "1",
            "User Test",
            "6900",
            "TGILMP",
            "081234567890",
            "test@agribank.com.vn",
            "0907809680",
            "Quản trị",
            "82204001",
        ],
        [],
    )
    users, _ = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert users[0].branch_code == "6900"


def test_resolve_role_for_assign_passthrough():
    from app.routers.users import _resolve_role_for_assign

    assert _resolve_role_for_assign("banca-seller") == "banca-seller"
    assert _resolve_role_for_assign("Quản trị") == "banca-admin"

