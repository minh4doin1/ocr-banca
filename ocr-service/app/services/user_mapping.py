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
    8: "unit_code",
}

_SSO_COLUMN_LABELS: list[dict[str, str]] = [
    {"col": "0", "field": "stt", "label": "STT"},
    {"col": "1", "field": "name", "label": "Họ và tên"},
    {"col": "2", "field": "department_name", "label": "Phòng/Đơn vị"},
    {"col": "3", "field": "ipcas_code", "label": "User IPCAS"},
    {"col": "4", "field": "cccd", "label": "Số CCCD"},
    {"col": "5", "field": "email", "label": "Email tại Agribank"},
    {"col": "6", "field": "phone", "label": "Số điện thoại"},
    {"col": "7", "field": "role", "label": "Phân quyền"},
    {"col": "8", "field": "unit_code", "label": "Ghi chú / Mã ĐV"},
]

_UNIT_CODE_RE = re.compile(r"^\d{6,10}$")
_DEPARTMENT_CODE_RE = re.compile(r"^(\d{4})\s+(.+)$")


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
    # OCR often returns combined/noisy role labels; map by intent keywords.
    if any(k in key for k in ("quan tri", "admin")):
        return "banca-admin"
    if any(k in key for k in ("phe duyet", "duyet vien", "controller")):
        return "banca-accounting-controller"
    if any(k in key for k in ("ke toan", "toan vien", "operator")):
        return "banca-accounting-operator"
    if any(k in key for k in ("dai ly", "seller", "sales")):
        return "banca-seller"
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
    email = (user.email or "").strip().lower()
    username = (user.username or "").strip()
    if email:
        user.email = email
        user.username = email
    elif username and "@" in username:
        user.email = username.lower()
        user.username = user.email

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
    user.ipcas_code = (user.ipcas_code or "").strip().upper()
    user.unit_code = re.sub(r"\s", "", user.unit_code or "")
    return user


def _parse_department_cell(text: str) -> tuple[str, str, str]:
    """Parse '6900 Hội sở' -> (full, branch_code, branch_name)."""
    t = (text or "").strip()
    if not t:
        return "", "", ""
    m = _DEPARTMENT_CODE_RE.match(t)
    if m:
        return t, m.group(1), m.group(2).strip()
    return t, "", ""


def _parse_unit_or_notes(raw: str) -> tuple[str, str]:
    """Cột Ghi chú: số 6-10 chữ số -> unit_code, còn lại -> notes."""
    compact = re.sub(r"\s", "", (raw or "").strip())
    if _UNIT_CODE_RE.fullmatch(compact):
        return compact, ""
    return "", (raw or "").strip()


def _derive_agribank_email(seed: str) -> str:
    """Build agribank email from IPCAS/username-like seed text."""
    raw = (seed or "").strip().lower()
    if not raw:
        return ""
    local = raw.split("@", 1)[0]
    local = re.sub(r"\s+", "", local)
    local = re.sub(r"[^a-z0-9._-]", "", local)
    if not local:
        return ""
    # Reject OCR garbage that often comes from domain fragments.
    if any(token in local for token in ("agribank", "bank.com.vn", "com.vn")):
        return ""
    # IPCAS/account seeds are expected to be compact identifiers, not 1-char or dotted domains.
    if "." in local:
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,24}", local):
        return ""
    return f"{local}@agribank.com.vn"


def get_sso_column_labels() -> list[dict[str, str]]:
    return list(_SSO_COLUMN_LABELS)


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
    return list(validate_user_field_errors(user).keys())


def validate_user_field_errors(user: KeycloakUserInput) -> dict[str, str]:
    """Trả map field -> thông báo lỗi tiếng Việt."""
    user = finalize_user(user)
    errors: dict[str, str] = {}
    data = user.model_dump()
    labels = settings.field_labels_vi

    for field in settings.user_required_fields_list:
        val = _field_value(data, field)
        if not val:
            label = labels.get(field, field)
            errors[field] = f"Thiếu {label}"

    # Keycloak profile hard-requirement in production realm.
    for field in ("branch_code", "phone", "cccd"):
        if not _field_value(data, field):
            label = labels.get(field, field)
            errors[field] = f"Thiếu {label}"

    if user.role and user.role not in settings.keycloak_valid_roles:
        errors["role"] = "Vai trò không hợp lệ"

    if user.cccd and not re.fullmatch(r"\d{12}", user.cccd):
        errors["cccd"] = "CCCD phải có 12 số"

    if user.phone and not re.fullmatch(r"0\d{8,10}", user.phone):
        errors["phone"] = "SĐT không hợp lệ"

    email = (user.email or "").strip()
    if email and not email.endswith("@agribank.com.vn"):
        errors["email"] = "Email phải thuộc @agribank.com.vn"

    return errors


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

    # Compatibility aliases for realms requiring these exact profile keys.
    if field_values["branch_code"]:
        attrs.setdefault("branchId", [str(field_values["branch_code"]).strip()])
    if field_values["phone"]:
        attrs.setdefault("phone", [str(field_values["phone"]).strip()])
    if field_values["cccd"]:
        attrs.setdefault("idNo", [str(field_values["cccd"]).strip()])

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
            if field == "unit_code":
                unit, _notes = _parse_unit_or_notes(raw)
                return unit
            return raw

        username = _val("username")
        email = _val("email")
        ipcas = _val("ipcas_code")

        # SSO fallback: email column can be blank OCR-wise, but IPCAS is present.
        if not email and ipcas:
            email = _derive_agribank_email(ipcas)
        if not username:
            username = email or _derive_agribank_email(ipcas)
        if not username and not email:
            continue

        first_name = _val("first_name")
        last_name = _val("last_name")
        name = _compose_name(first_name, last_name, _val("name"))

        dept_raw = _val("department_name")
        dept_name, parsed_branch_code, parsed_branch_name = _parse_department_cell(
            dept_raw
        )

        unit_code = _val("unit_code")
        notes = ""
        unit_idx = col_to_field.get("unit_code")
        if unit_idx is not None and unit_idx < len(row):
            parsed_unit, notes = _parse_unit_or_notes(row[unit_idx])
            if not unit_code:
                unit_code = parsed_unit

        user = KeycloakUserInput(
            username=username or email,
            email=email or username,
            name=name,
            first_name=first_name,
            last_name=last_name,
            cccd=_val("cccd"),
            branch_name=_val("branch_name") or parsed_branch_name,
            department_name=dept_name,
            branch_code=_val("branch_code") or parsed_branch_code,
            agent_code=_val("agent_code"),
            ipcas_code=ipcas,
            phone=_val("phone"),
            unit_code=unit_code,
            notes=notes,
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


def _sso_col_field_map(num_cols: int) -> dict[int, str]:
    """Map column index -> field name for SSO layout."""
    out: dict[int, str] = {0: "stt"}
    for col_idx, field in _SSO_DATA_COL_FIELDS.items():
        if col_idx < num_cols:
            out[col_idx] = field
    return out


def _validate_cell_for_field(field: str, text: str, confidence: float) -> str:
    """Return error message or empty string."""
    if field == "stt":
        return ""
    if not (text or "").strip():
        label = settings.field_labels_vi.get(field, field)
        return f"Thiếu {label}"
    if field == "cccd" and not re.fullmatch(r"\d{12}", re.sub(r"\s", "", text)):
        return "CCCD phải có 12 số"
    if field == "phone" and not re.fullmatch(r"0\d{8,10}", re.sub(r"\s", "", text)):
        return "SĐT không hợp lệ"
    if field in ("name", "first_name") and re.search(r"\d", text):
        return "Tên chứa chữ số"
    if field == "email" and "@" in text and not text.lower().endswith(
        "@agribank.com.vn"
    ):
        return "Email phải thuộc @agribank.com.vn"
    if confidence < settings.ocr_confidence_threshold:
        return "Độ tin cậy thấp"
    return ""


def validate_ocr_result(result: OcrResult) -> dict:
    """Validate all cells in OCR result. Returns errors and warnings lists."""
    from app.models.schemas import OcrCellValidationIssue

    errors: list[OcrCellValidationIssue] = []
    warnings: list[OcrCellValidationIssue] = []

    for page in result.pages:
        for table in page.tables:
            is_sso = table.table_kind == "sso_agribank" or (
                table.num_cols >= 7
                and not any(
                    c.row == 0 and "email" in (c.text or "").lower()
                    for c in table.cells
                )
            )
            col_fields = (
                _sso_col_field_map(table.num_cols)
                if is_sso
                else {}
            )
            if not is_sso:
                header_row = [
                    c for c in table.cells if c.row == 0
                ]
                alias_map = settings.keycloak_header_map_parsed
                for hc in header_row:
                    norm = _normalize(hc.text)
                    for field in _KNOWN_FIELDS:
                        if norm in alias_map.get(field, []):
                            col_fields[hc.col] = field
                            break

            for cell in table.cells:
                if cell.row == 0 and not is_sso:
                    continue
                field = col_fields.get(cell.col, "")
                if not field or field == "stt":
                    continue
                msg = _validate_cell_for_field(
                    field, cell.text or "", cell.confidence
                )
                if not msg:
                    continue
                issue = OcrCellValidationIssue(
                    page_number=page.page_number,
                    table_index=table.table_index,
                    row=cell.row,
                    col=cell.col,
                    field=field,
                    message=msg,
                    severity=(
                        "warn"
                        if "tin cậy" in msg.lower()
                        else "error"
                    ),
                )
                if issue.severity == "warn":
                    warnings.append(issue)
                else:
                    errors.append(issue)

    return {
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
    }
