"""
User Mapping — Chuyển kết quả OCR (bảng) sang danh sách KeycloakUserInput.
"""

from __future__ import annotations

import logging
import re
import unicodedata

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
    "ipcas_code",
    "phone",
    "unit_code",
    "role",
    "password",
)

# Layout form SSO Agribank (9 cột) khi postprocess đã bỏ dòng header.
_SSO_DATA_COL_FIELDS: dict[int, str] = {
    1: "name",
    2: "department_name",
    3: "ipcas_code",
    4: "cccd",
    5: "email",
    6: "phone",
    7: "role",
}


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _normalize_role_alias(text: str) -> str:
    s = _normalize(text)
    s = s.replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip()


def normalize_role(raw: str) -> str:
    """Chuẩn hoá vai trò nghiệp vụ -> tên client role Keycloak."""
    key = _normalize_role_alias(raw)
    if not key:
        return ""
    role_map = settings.keycloak_role_map_parsed
    if key in role_map:
        return role_map[key]
    if key in settings.keycloak_valid_roles:
        return key
    return ""


def _split_vn_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def finalize_user(user: KeycloakUserInput) -> KeycloakUserInput:
    """username=email, tách họ/tên, chuẩn hoá role."""
    email = (user.email or "").strip()
    username = (user.username or "").strip()
    if email:
        user.username = email
        if not user.email:
            user.email = email
    elif username and "@" in username:
        user.email = username

    if user.name.strip() and not (user.first_name.strip() and user.last_name.strip()):
        first, last = _split_vn_name(user.name)
        if not user.first_name.strip():
            user.first_name = first
        if not user.last_name.strip():
            user.last_name = last

    if user.role:
        user.role = normalize_role(user.role) or user.role.strip()

    user.cccd = re.sub(r"\s", "", user.cccd or "")
    user.phone = re.sub(r"\s", "", user.phone or "")
    return user


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
    if not row:
        return False
    return bool(re.match(r"^\d{1,3}$", (row[0] or "").strip()))


def _sso_data_col_map(num_cols: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for col_idx, field in _SSO_DATA_COL_FIELDS.items():
        if col_idx < num_cols:
            out[field] = col_idx
    return out


def _extract_cccd_from_cell(raw: str) -> str:
    compact = re.sub(r"\s", "", raw or "")
    m = re.search(r"\d{12}", compact)
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", compact)
    if len(digits) >= 12:
        return digits[:12]
    return (raw or "").strip()


def _resolve_col_map(matrix: list[list[str]]) -> tuple[dict[str, int], int]:
    if len(matrix) < 1:
        return {}, 0

    header = matrix[0]
    col_to_field = _map_header(header)
    if "username" in col_to_field or "email" in col_to_field:
        return col_to_field, 1

    if _is_sso_data_first_row(header) and len(header) >= 6:
        return _sso_data_col_map(len(header)), 0

    return col_to_field, 1


def _compose_name(first_name: str, last_name: str, full_name: str) -> str:
    if full_name.strip():
        return full_name.strip()
    parts = [p for p in (last_name.strip(), first_name.strip()) if p]
    return " ".join(parts)


def _field_value(data: dict, field: str) -> str:
    val = data.get(field, "")
    if field == "name":
        return _compose_name(
            data.get("first_name", ""),
            data.get("last_name", ""),
            data.get("name", ""),
        )
    if field == "role":
        return normalize_role(str(val or ""))
    return str(val or "").strip()


def validate_user_fields(user: KeycloakUserInput) -> list[str]:
    """Trả danh sách trường bắt buộc còn thiếu hoặc không hợp lệ."""
    user = finalize_user(user)
    missing: list[str] = []
    data = user.model_dump()

    for field in settings.user_required_fields_list:
        val = _field_value(data, field)
        if not val:
            missing.append(field)

    if user.role and user.role not in settings.keycloak_valid_roles:
        if "role" not in missing:
            missing.append("role")

    if user.cccd and not re.fullmatch(r"\d{12}", user.cccd):
        if "cccd" not in missing:
            missing.append("cccd")

    if user.phone and not re.fullmatch(r"0\d{8,10}", user.phone):
        if "phone" not in missing:
            missing.append("phone")

    return missing


def build_keycloak_attributes(user: KeycloakUserInput) -> dict[str, list[str]]:
    """Dựng Keycloak attributes từ user input."""
    attr_map = settings.keycloak_attribute_map_parsed
    attrs: dict[str, list[str]] = {}
    data = finalize_user(user).model_dump()

    field_values = {
        "cccd": data.get("cccd", ""),
        "name": _compose_name(
            data.get("first_name", ""),
            data.get("last_name", ""),
            data.get("name", ""),
        ),
        "branch_code": data.get("branch_code", ""),
        "agent_code": data.get("agent_code", ""),
        "branch_name": data.get("branch_name", ""),
        "department_name": data.get("department_name", ""),
        "ipcas_code": data.get("ipcas_code", ""),
        "phone": data.get("phone", ""),
        "unit_code": data.get("unit_code", ""),
    }

    for field, val in field_values.items():
        if not str(val or "").strip():
            continue
        key = attr_map.get(field, field)
        attrs[key] = [str(val).strip()]

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
    if "username" not in col_to_field and "email" not in col_to_field:
        warnings.append(
            f"Bảng {table.table_index + 1}: không tìm thấy cột email/username "
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
            if field == "role":
                return normalize_role(raw) or raw
            return raw

        username = _val("username")
        email = _val("email")
        if not username and not email:
            continue

        first_name = _val("first_name")
        last_name = _val("last_name")
        name = _compose_name(first_name, last_name, _val("name"))

        user = KeycloakUserInput(
            username=username or email,
            email=email or username,
            name=name,
            first_name=first_name,
            last_name=last_name,
            cccd=_val("cccd"),
            branch_name=_val("branch_name"),
            department_name=_val("department_name"),
            branch_code=_val("branch_code"),
            agent_code=_val("agent_code"),
            ipcas_code=_val("ipcas_code"),
            phone=_val("phone"),
            unit_code=_val("unit_code"),
            role=_val("role"),
            password=_val("password"),
        )
        user = finalize_user(user)
        if user.role and user.role not in settings.keycloak_valid_roles:
            warnings.append(
                f"Dòng {row_idx + 1}: vai trò '{user.role}' không hợp lệ."
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
