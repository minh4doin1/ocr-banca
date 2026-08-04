"""
User Mapping — Chuyển kết quả OCR (bảng) sang danh sách KeycloakUserInput.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher

from app.config import settings
from app.models.schemas import KeycloakUserInput, OcrResult, TableData
from app.services.email_reconcile import reconcile_agribank_email

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

# Layout form SSO Agribank Mẫu 01/SSO mới (10 cột) khi postprocess đã bỏ dòng header.
_SSO_DATA_COL_FIELDS_10: dict[int, str] = {
    1: "name",
    2: "branch_code",
    3: "branch_name",
    4: "ipcas_code",
    5: "cccd",
    6: "email",
    7: "phone",
    8: "role",
    9: "unit_code",
}

# Layout form SSO cũ (9 cột): Phòng/Đơn vị gộp mã CN + tên.
_SSO_DATA_COL_FIELDS_9: dict[int, str] = {
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
    {"col": "2", "field": "branch_code", "label": "Mã chi nhánh"},
    {"col": "3", "field": "branch_name", "label": "Tên Chi nhánh"},
    {"col": "4", "field": "ipcas_code", "label": "User IPCAS"},
    {"col": "5", "field": "cccd", "label": "Số CCCD"},
    {"col": "6", "field": "email", "label": "Email tại Agribank"},
    {"col": "7", "field": "phone", "label": "SĐT"},
    {"col": "8", "field": "role", "label": "Phân quyền"},
    {"col": "9", "field": "unit_code", "label": "Mã liên ngân hàng"},
]


def _sso_data_col_fields_for(num_cols: int) -> dict[int, str]:
    """Chọn layout cột theo số cột bảng (10 = mẫu mới, 9 = mẫu cũ)."""
    if num_cols >= 10:
        return _SSO_DATA_COL_FIELDS_10
    return _SSO_DATA_COL_FIELDS_9

_UNIT_CODE_RE = re.compile(r"^\d{6,10}$")
_DEPARTMENT_CODE_RE = re.compile(r"^(\d{4})\s+(.+)$")


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _normalize_header_key(text: str) -> str:
    """Chuẩn hóa tiêu đề cột: lower, bỏ dấu tiếng Việt (để khớp alias)."""
    s = str(text or "").strip().lower()
    s = s.replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def _normalize_phone(raw: str) -> str:
    """Bỏ khoảng trắng, dấu chấm, gạch ngang trong SĐT."""
    return re.sub(r"[\s.\-]", "", (raw or "").strip())


def _header_cell_matches_field(norm_cell: str, aliases: list[str]) -> bool:
    if not norm_cell:
        return False
    # Ô dữ liệu (email, số thuần) — không phải tiêu đề cột
    if "@" in norm_cell:
        return False
    if re.fullmatch(r"[\d.\s\-]+", norm_cell):
        return False
    if norm_cell in aliases:
        return True
    for alias in aliases:
        if len(alias) < 4:
            continue
        if alias == norm_cell:
            return True
        # Tránh "chi nhanh" khớp nhầm header "ma chi nhanh" (thuộc branch_code).
        if alias in norm_cell:
            if norm_cell.startswith("ma ") and alias in {"chi nhanh", "chi nhanh"}:
                continue
            if alias.startswith("ten ") and not norm_cell.startswith("ten "):
                continue
            return True
        if norm_cell in alias and len(norm_cell) >= 4:
            return True
    return False


def _map_header_sufficient(col_to_field: dict[str, int]) -> bool:
    """Cần ít nhất 2 trường quan trọng mới coi là dòng tiêu đề."""
    keys = ("username", "email", "ipcas_code", "name", "cccd", "phone", "role")
    return sum(1 for k in keys if k in col_to_field) >= 2


def _is_column_number_row(row: list[str]) -> bool:
    """Dòng đánh số cột (1)(2)… — không nhầm với dòng dữ liệu STT=1."""
    vals = [str(c or "").strip() for c in row if str(c or "").strip()]
    if len(vals) < 3:
        return False
    paren = re.compile(r"^\(?\d{1,2}\)?$")
    hits = sum(1 for v in vals if paren.match(v))
    # Dòng số cột: hầu hết ô chỉ là số thứ tự cột, không có tên/email
    return hits >= 3 and hits >= len(vals) * 0.8


def _strip_sso_preamble_rows(matrix: list[list[str]]) -> list[list[str]]:
    """Bỏ dòng tiêu đề SSO và dòng đánh số cột (1)(2)… ở đầu bảng."""
    rows = list(matrix)
    while rows:
        if _is_column_number_row(rows[0]):
            rows = rows[1:]
            continue
        hits = 0
        alias_map = settings.keycloak_header_map_parsed
        for cell in rows[0][:12]:
            norm = _normalize_header_key(cell)
            if not norm:
                continue
            for field in _KNOWN_FIELDS:
                if _header_cell_matches_field(norm, alias_map.get(field, [])):
                    hits += 1
                    break
        if hits >= 4:
            rows = rows[1:]
            continue
        break
    return rows


def _normalize_role_alias(text: str) -> str:
    s = _normalize(text)
    s = s.replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip()


_ROLE_PHRASE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"phe\s*duyet|duyet\s*vien|controller", re.I), "banca-accounting-controller"),
    (re.compile(r"dai\s*l[iy]|dai\s*ly\s*vien|seller|sales", re.I), "banca-seller"),
    (re.compile(r"ke\s*toan|kê\s*toan|toan\s*vien|kt\s*vien|operator", re.I), "banca-accounting-operator"),
    (re.compile(r"quan\s*tri|admin|qtv", re.I), "banca-admin"),
]

_ROLE_CANONICAL_ASCII = (
    ("phe duyet vien", "banca-accounting-controller"),
    ("dai ly vien", "banca-seller"),
    ("ke toan vien", "banca-accounting-operator"),
    ("quan tri", "banca-admin"),
)


def _fuzzy_role_match(key: str, valid: set[str]) -> str:
    if not key or len(key) < 3:
        return ""
    best_role = ""
    best_score = 0.0
    threshold = settings.ocr_sso_role_fuzzy_threshold
    for canonical, role in _ROLE_CANONICAL_ASCII:
        score = SequenceMatcher(None, key, canonical).ratio()
        if score >= threshold and score > best_score and role in valid:
            best_score = score
            best_role = role
    return best_role


def extract_roles_from_ocr(raw: str) -> list[str]:
    """Extract all Keycloak roles from OCR role text (with or without delimiters)."""
    if not str(raw or "").strip():
        return []

    role_map = settings.keycloak_role_map_parsed
    valid = set(settings.keycloak_valid_roles)
    seen: set[str] = set()
    out: list[str] = []

    def _add(resolved: str) -> None:
        if resolved and resolved in valid and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)

    text = str(raw).strip()
    # Explicit delimiters and Vietnamese "và"
    parts = re.split(r"[;,/|]+|\s+và\s+|\s+va\s+", text, flags=re.IGNORECASE)
    for part in parts:
        key = _normalize_role_alias(part)
        if not key:
            continue
        if key in role_map:
            _add(role_map[key])
            continue
        fuzzy = _fuzzy_role_match(key, valid)
        if fuzzy:
            _add(fuzzy)

    # Phrase scan on full string — preserve left-to-right order
    full_key = _normalize_role_alias(text)
    positioned: list[tuple[int, str]] = []
    for pattern, role in _ROLE_PHRASE_PATTERNS:
        for m in pattern.finditer(full_key):
            positioned.append((m.start(), role))
    positioned.sort(key=lambda x: x[0])
    for _, role in positioned:
        _add(role)

    if out:
        return out

    # Keyword fallback on whole string
    if any(k in full_key for k in ("quan tri", "admin", "qtv")):
        _add("banca-admin")
    if any(k in full_key for k in ("phe duyet", "duyet vien", "controller")):
        _add("banca-accounting-controller")
    if any(k in full_key for k in ("ke toan", "toan vien", "kt vien", "operator")):
        _add("banca-accounting-operator")
    if any(k in full_key for k in ("dai ly", "dai li", "seller", "sales")):
        _add("banca-seller")

    if not out:
        for part in parts:
            key = _normalize_role_alias(part)
            if key in valid:
                _add(key)

    return out


def normalize_role(raw: str) -> str:
    """Chuẩn hoá vai trò nghiệp vụ -> tên client role Keycloak."""
    roles = normalize_roles(raw)
    return roles[0] if roles else ""


def normalize_roles(raw: str) -> list[str]:
    """Tách và chuẩn hoá nhiều role (phân tách ; , / | hoặc phrase scan)."""
    return extract_roles_from_ocr(raw)


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

    if user.roles:
        user.roles = normalize_roles(";".join(user.roles))
    elif user.role:
        user.roles = normalize_roles(user.role)
    else:
        user.roles = []
    if user.roles:
        user.role = user.roles[0]
    elif user.role:
        user.role = normalize_role(user.role) or user.role.strip()
    else:
        user.role = ""

    user.cccd = re.sub(r"\s", "", user.cccd or "")
    user.phone = re.sub(r"\s", "", user.phone or "")
    user.ipcas_code = (user.ipcas_code or "").strip().upper()
    user.unit_code = re.sub(r"\s", "", user.unit_code or "")
    return user


def _parse_branch_code_digits(raw: str) -> str:
    """Extract 4-digit branch code from cell text (e.g. 6900, 6900.0, '6900 Hội sở')."""
    t = (raw or "").strip().lstrip("'").strip()
    if not t:
        return ""
    # Bỏ hậu tố .0 từ số Excel / paste values
    compact = re.sub(r"\s", "", t)
    compact = re.sub(r"\.0+$", "", compact)
    m = re.fullmatch(r"(\d{4})", compact)
    if m:
        return m.group(1)
    m = _DEPARTMENT_CODE_RE.match(t)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4})\b", t)
    return m.group(1) if m else ""


def _parse_department_cell(text: str) -> tuple[str, str, str]:
    """Parse '6900 Hội sở' -> (full, branch_code, branch_name)."""
    t = (text or "").strip()
    if not t:
        return "", "", ""
    m = _DEPARTMENT_CODE_RE.match(t)
    if m:
        return t, m.group(1), m.group(2).strip()
    code = _parse_branch_code_digits(t)
    if code:
        return t, code, ""
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
    try:
        from app.services.template_service import get_template_or_default

        profile = get_template_or_default("sso-agribank")
        if profile.table.columns:
            return [
                {
                    "col": str(c.index),
                    "field": c.field,
                    "label": c.header or c.field,
                }
                for c in profile.table.columns
            ]
    except Exception:
        pass
    return list(_SSO_COLUMN_LABELS)


def _build_matrix(table: TableData) -> list[list[str]]:
    if table.num_rows <= 0 or table.num_cols <= 0:
        return []
    matrix = [["" for _ in range(table.num_cols)] for _ in range(table.num_rows)]
    for cell in table.cells:
        if 0 <= cell.row < table.num_rows and 0 <= cell.col < table.num_cols:
            matrix[cell.row][cell.col] = (cell.text or "").strip()
    return matrix


def _map_header(
    header: list[str],
    alias_override: dict[str, list[str]] | None = None,
) -> dict[str, int]:
    alias_map = alias_override or settings.keycloak_header_map_parsed
    col_to_field: dict[str, int] = {}
    for col_idx, title in enumerate(header):
        norm = _normalize_header_key(title)
        if not norm:
            continue
        best_field = ""
        best_score = 0
        for field in _KNOWN_FIELDS:
            for alias in alias_map.get(field, []):
                if not _header_cell_matches_field(norm, [alias]):
                    continue
                score = len(alias)
                if alias == norm:
                    score += 1000
                # Ưu tiên branch_code cho tiêu đề "mã chi nhánh" / "mã cn"
                if field == "branch_code" and (
                    norm.startswith("ma ") or norm in {"ma cn", "branch code"}
                ):
                    score += 100
                if field == "branch_name" and norm.startswith("ma "):
                    score -= 200
                if score > best_score:
                    best_field = field
                    best_score = score
        if best_field and best_field not in col_to_field:
            col_to_field[best_field] = col_idx
    return col_to_field


def _is_sso_data_first_row(row: list[str]) -> bool:
    if not row:
        return False
    if re.match(r"^\d{1,3}$", (row[0] or "").strip()):
        return True
    if len(row) < 5:
        return False
    ipcas = (row[4] or "").strip().upper()
    if ipcas.startswith("QSO"):
        return True
    if len(row) < 8:
        return False
    name = (row[1] or "").strip()
    if not name or name.isascii() or len(name) < 3:
        return False
    ipcas = (row[4] or "").strip()
    cccd = re.sub(r"\D", "", row[5] or "")
    email = (row[6] or "").strip().lower()
    if len(cccd) >= 9:
        return True
    if ipcas and re.fullmatch(r"[A-Z0-9]{4,16}", ipcas.upper()):
        return True
    if "@agribank" in email or email.endswith("@agribank.com.vn"):
        return True
    return False


def _looks_like_truncated_sso_10(matrix: list[list[str]], start_row: int = 0) -> bool:
    """9 cột nhưng thực chất là mẫu 10 cột bị cắt cột cuối (Paste Values làm trống).

    Dấu hiệu: col2 = mã CN 4 số, col3 = tên CN (không phải IPCAS), col4 = IPCAS.
    """
    samples = 0
    hits = 0
    for row in matrix[start_row : start_row + 20]:
        if len(row) < 8:
            continue
        if _is_column_number_row(row):
            continue
        c2 = re.sub(r"\.0+$", "", re.sub(r"\s", "", (row[2] or "").strip()))
        c3 = (row[3] or "").strip()
        c4 = (row[4] or "").strip().upper()
        if not c2 and not c3:
            continue
        samples += 1
        code_ok = bool(re.fullmatch(r"\d{4}", c2))
        c3_as_ipcas = bool(re.fullmatch(r"[A-Z0-9]{4,16}", c3.upper())) and c3.isascii()
        c4_as_ipcas = bool(re.fullmatch(r"[A-Z0-9]{4,16}", c4))
        if code_ok and not c3_as_ipcas and c4_as_ipcas:
            hits += 1
    return samples >= 2 and hits >= max(2, (samples + 1) // 2)


def _sso_data_col_map(num_cols: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for col_idx, field in _sso_data_col_fields_for(num_cols).items():
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


def _resolve_col_map(
    matrix: list[list[str]],
    profile: "TemplateProfile | None" = None,
) -> tuple[dict[str, int], int]:
    if len(matrix) < 1:
        return {}, 0

    # Explicit template column map (non-SSO or custom)
    if profile is not None and profile.id != "sso-agribank" and profile.table.columns:
        from app.services.template_service import field_to_col_index_map

        col_to_field = field_to_col_index_map(profile)
        if col_to_field:
            header_row = max(0, min(profile.table.header_row, len(matrix) - 1))
            # If first rows look like headers matching template, skip them
            data_start = header_row + 1
            while data_start < len(matrix) and _is_column_number_row(matrix[data_start]):
                data_start += 1
            # Also skip a header-like row when values match template headers
            if data_start < len(matrix):
                row = matrix[header_row]
                hits = 0
                for col in profile.table.columns:
                    if col.index < len(row) and col.header:
                        if _normalize_header_key(row[col.index]) == _normalize_header_key(
                            col.header
                        ):
                            hits += 1
                if hits >= max(2, len(profile.table.columns) // 3):
                    data_start = max(data_start, header_row + 1)
            return col_to_field, min(data_start, len(matrix))

    scan_limit = min(6, len(matrix))
    alias_override = None
    if profile is not None and profile.table.header_aliases:
        alias_override = profile.table.header_aliases

    for row_idx in range(scan_limit):
        row = matrix[row_idx]
        if _is_column_number_row(row):
            continue
        col_to_field = _map_header(row, alias_override=alias_override)
        if _map_header_sufficient(col_to_field):
            data_start = row_idx + 1
            while data_start < len(matrix) and _is_column_number_row(matrix[data_start]):
                data_start += 1
            # Header có mã CN mà không map được branch_code (bị nhầm branch_name)
            if "branch_code" not in col_to_field:
                for ci, title in enumerate(row):
                    norm = _normalize_header_key(title)
                    if norm in {"ma chi nhanh", "ma cn", "branch code", "ma chinhanh"}:
                        col_to_field["branch_code"] = ci
                        break
            return col_to_field, data_start

    # Fallback: positional map from template columns (SSO layouts)
    if profile is not None and profile.table.columns:
        from app.services.template_service import field_to_col_index_map

        for row_idx, row in enumerate(matrix):
            if _is_column_number_row(row):
                continue
            if _is_sso_data_first_row(row) and len(row) >= 8:
                # Prefer classic SSO positional map for builtin compatibility
                if profile.id == "sso-agribank" or profile.ocr.sso_enhance:
                    return _sso_data_col_map(len(row)), row_idx
                return field_to_col_index_map(profile), row_idx

    for row_idx, row in enumerate(matrix):
        if _is_column_number_row(row):
            continue
        if _is_sso_data_first_row(row) and len(row) >= 8:
            n = len(row)
            if n == 9 and _looks_like_truncated_sso_10(matrix, row_idx):
                # Dùng map 10 cột trên dữ liệu 9 cột (cột unit có thể trống).
                return _sso_data_col_map(10), row_idx
            return _sso_data_col_map(n), row_idx

    return {}, 0


def table_has_user_columns(matrix: list[list[str]]) -> bool:
    """Bảng có đủ cột để map user (email/username/ipcas hoặc layout SSO)."""
    if len(matrix) < 1:
        return False
    col_map, _ = _resolve_col_map(matrix)
    return any(f in col_map for f in ("username", "email", "ipcas_code"))


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
        roles = data.get("roles") or normalize_roles(str(val or ""))
        return roles[0] if roles else normalize_role(str(val or ""))
    return str(val or "").strip()


def validate_user_fields(
    user: KeycloakUserInput, *, partial: bool = False
) -> list[str]:
    """Trả danh sách trường bắt buộc còn thiếu hoặc không hợp lệ."""
    return list(validate_user_field_errors(user, partial=partial).keys())


def validate_user_field_errors(
    user: KeycloakUserInput, *, partial: bool = False
) -> dict[str, str]:
    """Trả map field -> thông báo lỗi tiếng Việt.

    partial=True (mode edit): chỉ bắt username + format các field đã điền.
    partial=False: bắt đủ cột theo USER_REQUIRED_FIELDS (hành vi tạo lô).
    """
    user = finalize_user(user)
    errors: dict[str, str] = {}
    data = user.model_dump()
    labels = settings.field_labels_vi

    username = (user.username or "").strip()
    if not username:
        errors["username"] = f"Thiếu {labels.get('username', 'username')}"

    if not partial:
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

        roles = list(user.roles) if user.roles else (
            normalize_roles(user.role) if user.role else []
        )
        if not roles and "role" in settings.user_required_fields_list:
            errors["role"] = "Thiếu vai trò"
    else:
        roles = list(user.roles) if user.roles else (
            normalize_roles(user.role) if user.role else []
        )

    for r in roles:
        if r not in settings.keycloak_valid_roles:
            errors["role"] = f"Vai trò không hợp lệ: {r}"
            break

    if user.cccd and not re.fullmatch(r"\d{12}", user.cccd):
        errors["cccd"] = "CCCD phải có 12 số"

    if user.phone and not re.fullmatch(r"0\d{8,10}", _normalize_phone(user.phone)):
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
    profile: "TemplateProfile | None" = None,
) -> tuple[list[KeycloakUserInput], list[str]]:
    warnings: list[str] = []
    matrix = _build_matrix(table)
    if len(matrix) < 1:
        return [], warnings

    col_to_field, data_start = _resolve_col_map(matrix, profile=profile)
    if not any(f in col_to_field for f in ("username", "email", "ipcas_code")):
        warnings.append(
            f"Bảng {table.table_index + 1}: không tìm thấy cột email/username/ipcas "
            f"(cần header hoặc form SSO 10 cột). Bỏ qua bảng này."
        )
        return [], warnings

    users: list[KeycloakUserInput] = []
    for row_idx in range(data_start, len(matrix)):
        row = matrix[row_idx]
        if _is_column_number_row(row):
            continue

        def _val(field: str) -> str:
            idx = col_to_field.get(field)
            if idx is None or idx >= len(row):
                return ""
            raw = row[idx].strip()
            if field == "cccd":
                return _extract_cccd_from_cell(raw)
            if field == "branch_code":
                return _parse_branch_code_digits(raw)
            if field == "phone":
                return _normalize_phone(raw)
            if field == "role":
                return raw
            if field == "unit_code":
                unit, _notes = _parse_unit_or_notes(raw)
                return unit
            return raw

        username = _val("username")
        email = _val("email")
        ipcas = _val("ipcas_code")

        reconciled_email, _email_src = reconcile_agribank_email(email, ipcas)
        if reconciled_email:
            email = reconciled_email
        elif not email and ipcas:
            email = _derive_agribank_email(ipcas)
        if not username:
            username = email or _derive_agribank_email(ipcas)
        if not username and not email:
            stt = _val("stt") or str(row_idx + 1)
            name_hint = _compose_name(_val("first_name"), _val("last_name"), _val("name"))
            hint = f" (STT {stt}" + (f", {name_hint}" if name_hint else "") + ")"
            warnings.append(
                f"Dòng {row_idx + 1}{hint}: bỏ qua — thiếu email/IPCAS/username."
            )
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

        role_raw = _val("role")
        parsed_roles = normalize_roles(role_raw)
        branch_code_val = _val("branch_code") or parsed_branch_code
        if not branch_code_val and dept_raw:
            branch_code_val = _parse_branch_code_digits(dept_raw)

        user = KeycloakUserInput(
            username=username or email,
            email=email or username,
            name=name,
            first_name=first_name,
            last_name=last_name,
            cccd=_val("cccd"),
            branch_name=_val("branch_name") or parsed_branch_name,
            department_name=dept_name or _val("branch_name") or parsed_branch_name,
            branch_code=branch_code_val,
            agent_code=_val("agent_code"),
            ipcas_code=ipcas,
            phone=_val("phone"),
            unit_code=unit_code,
            notes=notes,
            role_raw=role_raw,
            role=parsed_roles[0] if parsed_roles else role_raw.strip(),
            roles=parsed_roles,
            password=_val("password"),
        )
        user = finalize_user(user)
        invalid_roles = [r for r in user.roles if r not in settings.keycloak_valid_roles]
        if invalid_roles:
            warnings.append(
                f"Dòng {row_idx + 1}: vai trò không hợp lệ: {', '.join(invalid_roles)}."
            )
        user.missing_fields = validate_user_fields(user)
        users.append(user)

    return users, warnings


def map_result_to_users(
    result: OcrResult,
    template_id: str | None = None,
) -> tuple[list[KeycloakUserInput], list[str]]:
    all_users: list[KeycloakUserInput] = []
    warnings: list[str] = list(getattr(result, "warnings", None) or [])
    seen: set[str] = set()

    profile = None
    try:
        from app.services.template_service import get_template_or_default

        profile = get_template_or_default(template_id)
    except Exception:
        profile = None

    for page in result.pages:
        for table in page.tables:
            users, table_warnings = map_table_to_users(table, profile=profile)
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
    for col_idx, field in _sso_data_col_fields_for(num_cols).items():
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
    if field == "phone" and not re.fullmatch(r"0\d{8,10}", _normalize_phone(text)):
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
                    norm = _normalize_header_key(hc.text)
                    for field in _KNOWN_FIELDS:
                        if _header_cell_matches_field(norm, alias_map.get(field, [])):
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
