"""
Tests cho tính năng tạo lô user Keycloak.

- Map kết quả OCR -> danh sách KeycloakUserInput.
- Các nhánh on_conflict trong _provision_one (mock KeycloakClient).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.models.schemas import (
    CellData,
    KeycloakUserInput,
    OcrResult,
    OnConflictAction,
    PageResult,
    ProvisionStatus,
    TableData,
)
from app.routers.users import _provision_one
from app.services.user_mapping import map_result_to_users


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


# ──────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────


def test_map_result_basic():
    """Header khớp map -> tạo user đúng field."""
    table = _make_table(
        ["Username", "Email", "Họ", "Tên"],
        [
            ["nguyenvana", "a@example.com", "Nguyễn", "Văn A"],
            ["tranthib", "b@example.com", "Trần", "Thị B"],
        ],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert len(users) == 2
    assert warnings == []
    assert users[0].username == "nguyenvana"
    assert users[0].email == "a@example.com"
    assert users[0].last_name == "Nguyễn"
    assert users[0].first_name == "Văn A"


def test_map_skips_empty_username_rows():
    """Dòng không có username bị bỏ qua."""
    table = _make_table(
        ["Username", "Email"],
        [
            ["user1", "u1@example.com"],
            ["", "ghost@example.com"],
        ],
    )
    users, _ = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert users[0].username == "user1"


def test_map_warns_when_no_username_column():
    """Không có cột username -> cảnh báo, không tạo user."""
    table = _make_table(
        ["Email", "Họ"],
        [["x@example.com", "Nguyễn"]],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert users == []
    assert any("username" in w for w in warnings)


def test_map_dedupes_username():
    """Username trùng chỉ giữ bản ghi đầu tiên và cảnh báo."""
    table = _make_table(
        ["Username", "Email"],
        [
            ["dup", "first@example.com"],
            ["dup", "second@example.com"],
        ],
    )
    users, warnings = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert users[0].email == "first@example.com"
    assert any("trùng" in w.lower() for w in warnings)


# ──────────────────────────────────────────────────────────────
# on_conflict branches
# ──────────────────────────────────────────────────────────────


def _client_new_user() -> MagicMock:
    client = MagicMock()
    client.find_user_by_username.return_value = None
    client.create_user.return_value = "new-id-123"
    return client


def _client_existing_user() -> MagicMock:
    client = MagicMock()
    client.find_user_by_username.return_value = {"id": "existing-id", "username": "u"}
    client.reset_otp.return_value = 1
    return client


def _user(**kwargs) -> KeycloakUserInput:
    data = {"username": "u", "name": "Nguyễn Văn A", "cccd": "001234567890"}
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
    assert result.user_id == "new-id-123"
    client.create_user.assert_called_once()
    kwargs = client.create_user.call_args.kwargs
    assert kwargs["required_actions"] == ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]


def test_provision_skip_existing():
    client = _client_existing_user()
    user = _user()
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.SKIPPED
    client.create_user.assert_not_called()
    client.reset_password.assert_not_called()
    client.reset_otp.assert_not_called()


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
    client.ensure_required_actions.assert_called_once_with(
        "existing-id", ["UPDATE_PASSWORD"]
    )
    client.reset_otp.assert_not_called()


def test_provision_reset_otp():
    client = _client_existing_user()
    user = _user()
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.RESET_OTP,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_otp.assert_called_once_with("existing-id")
    client.reset_password.assert_not_called()


def test_provision_reset_both():
    client = _client_existing_user()
    user = _user(password="Both@123")
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.RESET_BOTH,
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_password.assert_called_once()
    client.reset_otp.assert_called_once_with("existing-id")


def test_per_user_on_conflict_overrides_batch_default():
    """on_conflict của user ghi đè mặc định của lô."""
    client = _client_existing_user()
    user = _user(on_conflict=OnConflictAction.RESET_OTP)
    result = _provision_one(
        client,
        user,
        temporary=True,
        on_conflict=OnConflictAction.SKIP,  # mặc định lô là skip
        default_required_actions=[],
    )
    assert result.status == ProvisionStatus.UPDATED
    client.reset_otp.assert_called_once()


def test_map_cccd_and_name_fields():
    """Map cột CCCD và Họ tên."""
    table = _make_table(
        ["Username", "Họ tên", "CCCD", "Email"],
        [["user1", "Nguyễn Văn A", "001234567890", "a@example.com"]],
    )
    users, _ = map_result_to_users(_make_result(table))
    assert len(users) == 1
    assert users[0].name == "Nguyễn Văn A"
    assert users[0].cccd == "001234567890"


def test_validate_user_fields_missing_cccd():
    from app.services.user_mapping import build_keycloak_attributes, validate_user_fields

    user = KeycloakUserInput(username="u", name="Test User")
    missing = validate_user_fields(user)
    assert "cccd" in missing

    user2 = KeycloakUserInput(
        username="u",
        name="Test",
        cccd="001234567890",
        branch_code="001",
        agent_code="DL1",
    )
    attrs = build_keycloak_attributes(user2)
    assert attrs["cccd"] == ["001234567890"]
    assert attrs["branchCode"] == ["001"]
    assert attrs["agentCode"] == ["DL1"]
    assert attrs["fullName"] == ["Test"]

