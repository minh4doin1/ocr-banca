"""
User Mapping — Chuyển kết quả OCR (bảng) sang danh sách KeycloakUserInput.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.models.schemas import KeycloakUserInput, OcrResult, TableData

logger = logging.getLogger(__name__)

_KNOWN_FIELDS = (
    "username",
    "email",
    "name",
    "first_name",
    "last_name",
    "cccd",
    "branch_name",
    "department_name",
    "branch_code",
    "agent_code",
    "password",
)


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _build_matrix(table: TableData) -> list[list[str]]:
    if table.num_rows <= 0 or table.num_cols <= 0:
        return []
    matrix = [["" for _ in range(table.num_cols)] for _ in range(table.num_rows)]
    for cell in table.cells:
        if 0 <= cell.row < table.num_rows and 0 <= cell.col < table.num_cols:
            matrix[cell.row][cell.col] = (cell.text or "").strip()
    return matrix


def _map_header(header: list[str]) -> dict[str, int]:
    alias_map = settings.keycloak_header_map_parsed
    col_to_field: dict[str, int] = {}
    for col_idx, title in enumerate(header):
        norm = _normalize(title)
        if not norm:
            continue
        for field in _KNOWN_FIELDS:
            aliases = alias_map.get(field, [])
            if norm in aliases and field not in col_to_field:
                col_to_field[field] = col_idx
                break
    return col_to_field


def _compose_name(first_name: str, last_name: str, full_name: str) -> str:
    if full_name.strip():
        return full_name.strip()
    parts = [p for p in (last_name.strip(), first_name.strip()) if p]
    return " ".join(parts)


def validate_user_fields(user: KeycloakUserInput) -> list[str]:
    """Trả danh sách trường bắt buộc còn thiếu."""
    missing: list[str] = []
    data = user.model_dump()
    for field in settings.user_required_fields_list:
        val = data.get(field, "")
        if field == "name":
            val = _compose_name(
                data.get("first_name", ""),
                data.get("last_name", ""),
                data.get("name", ""),
            )
        if not str(val or "").strip():
            missing.append(field)
    return missing


def build_keycloak_attributes(user: KeycloakUserInput) -> dict[str, list[str]]:
    """Dựng Keycloak attributes từ user input."""
    attrs: dict[str, list[str]] = {}
    name = _compose_name(user.first_name, user.last_name, user.name)
    if user.cccd:
        attrs["cccd"] = [user.cccd.strip()]
    if name:
        attrs["fullName"] = [name]
    if user.branch_code:
        attrs["branchCode"] = [user.branch_code.strip()]
    if user.agent_code:
        attrs["agentCode"] = [user.agent_code.strip()]
    if user.branch_name:
        attrs["branchName"] = [user.branch_name.strip()]
    if user.department_name:
        attrs["departmentName"] = [user.department_name.strip()]
    if user.attributes:
        for k, v in user.attributes.items():
            if v:
                attrs[k] = v
    return attrs


def map_table_to_users(
    table: TableData,
) -> tuple[list[KeycloakUserInput], list[str]]:
    warnings: list[str] = []
    matrix = _build_matrix(table)
    if len(matrix) < 2:
        return [], warnings

    header = matrix[0]
    col_to_field = _map_header(header)

    if "username" not in col_to_field:
        warnings.append(
            f"Bảng {table.table_index + 1}: không tìm thấy cột 'username'. "
            "Bỏ qua bảng này."
        )
        return [], warnings

    users: list[KeycloakUserInput] = []
    for row_idx in range(1, len(matrix)):
        row = matrix[row_idx]

        def _val(field: str) -> str:
            idx = col_to_field.get(field)
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        username = _val("username")
        if not username:
            continue

        first_name = _val("first_name")
        last_name = _val("last_name")
        name = _compose_name(first_name, last_name, _val("name"))

        user = KeycloakUserInput(
            username=username,
            email=_val("email"),
            name=name,
            first_name=first_name,
            last_name=last_name,
            cccd=_val("cccd").replace(" ", ""),
            branch_name=_val("branch_name"),
            department_name=_val("department_name"),
            branch_code=_val("branch_code"),
            agent_code=_val("agent_code"),
            password=_val("password"),
        )
        user.missing_fields = validate_user_fields(user)
        users.append(user)

    return users, warnings


def map_result_to_users(
    result: OcrResult,
) -> tuple[list[KeycloakUserInput], list[str]]:
    all_users: list[KeycloakUserInput] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for page in result.pages:
        for table in page.tables:
            users, table_warnings = map_table_to_users(table)
            warnings.extend(table_warnings)
            for user in users:
                key = user.username.strip().lower()
                if key in seen:
                    warnings.append(
                        f"Username trùng: '{user.username}' (giữ bản đầu)."
                    )
                    continue
                seen.add(key)
                all_users.append(user)

    return all_users, warnings
