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

# Layout form SSO Agribank (9 cột) khi postprocess đã bỏ dòng header.
_SSO_DATA_COL_FIELDS: dict[int, str] = {
    1: "name",
    2: "department_name",
    3: "username",
    4: "cccd",
    5: "email",
}


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


def _is_sso_data_first_row(row: list[str]) -> bool:
    """True khi dòng đầu là STT số (bảng SSO không còn dòng header)."""
    import re

    if not row:
        return False
    return bool(re.match(r"^\d{1,3}$", (row[0] or "").strip()))


def _sso_data_col_map(num_cols: int) -> dict[str, int]:
    """Map cột cố định form SSO (STT | Họ tên | Phòng | IPCAS | CCCD | Email | …)."""
    out: dict[str, int] = {}
    for col_idx, field in _SSO_DATA_COL_FIELDS.items():
        if col_idx < num_cols:
            out[field] = col_idx
    return out


def _extract_cccd_from_cell(raw: str) -> str:
    """Lấy 12 chữ số CCCD từ ô có thể kèm ngày cấp."""
    import re

    compact = re.sub(r"\s", "", raw or "")
    m = re.search(r"\d{12}", compact)
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", compact)
    if len(digits) >= 12:
        return digits[:12]
    return (raw or "").strip()


def _resolve_col_map(matrix: list[list[str]]) -> tuple[dict[str, int], int]:
    """
    Trả (col_to_field, data_start_row).

    Bảng SSO sau postprocess thường không còn header — dòng 0 đã là dữ liệu STT=1.
    """
    if len(matrix) < 1:
        return {}, 0

    header = matrix[0]
    col_to_field = _map_header(header)
    if "username" in col_to_field:
        return col_to_field, 1

    if _is_sso_data_first_row(header) and len(header) >= 6:
        return _sso_data_col_map(len(header)), 0

    return col_to_field, 1


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
    if len(matrix) < 1:
        return [], warnings

    col_to_field, data_start = _resolve_col_map(matrix)
    if "username" not in col_to_field:
        warnings.append(
            f"Bảng {table.table_index + 1}: không tìm thấy cột 'username' "
            f"(cần header hoặc form SSO 9 cột). Bỏ qua bảng này."
        )
        return [], warnings

    users: list[KeycloakUserInput] = []
    for row_idx in range(data_start, len(matrix)):
        row = matrix[row_idx]

        def _val(field: str) -> str:
            idx = col_to_field.get(field)
            if idx is None or idx >= len(row):
                return ""
            raw = row[idx].strip()
            if field == "cccd":
                return _extract_cccd_from_cell(raw)
            return raw

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
            cccd=_val("cccd"),
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
