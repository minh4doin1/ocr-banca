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
from app.services.user_mapping import map_result_to_users, normalize_role


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
    return client


def _client_existing_user() -> MagicMock:
    client = MagicMock()
    client.find_user_by_username.return_value = {"id": "existing-id", "username": "u"}
    client.reset_otp.return_value = 1
    client.get_client_by_client_id.return_value = {"id": "client-uuid"}
    client.get_client_role.return_value = {"id": "role-id", "name": "banca-seller"}
    client.get_user_client_roles.return_value = []
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
    client.assign_client_roles.assert_called_once()
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
    client.assign_client_roles.assert_called_once()
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
    client.reset_password.assert_called_once_with(
        "existing-id", "New@123", temporary=True
    )


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
    client.get_client_by_client_id.side_effect = users_router.KeycloakError(
        "Tra client 'banca' thất bại (HTTP 403)"
    )
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
