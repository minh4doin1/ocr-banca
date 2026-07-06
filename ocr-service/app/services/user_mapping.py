"""
User Mapping — Chuyển kết quả OCR (bảng) sang danh sách KeycloakUserInput.

Dùng dòng đầu mỗi bảng làm header, map tiêu đề cột sang field Keycloak theo
cấu hình KEYCLOAK_HEADER_MAP (settings.keycloak_header_map_parsed).
"""

from __future__ import annotations

import logging

from app.config import settings
from app.models.schemas import KeycloakUserInput, OcrResult, TableData

logger = logging.getLogger(__name__)

# Các field được nhận dạng từ header (khớp key trong header map).
_KNOWN_FIELDS = ("username", "email", "first_name", "last_name", "password")


def _normalize(text: str) -> str:
    """Chuẩn hóa tiêu đề cột để so khớp: lowercase, gộp khoảng trắng."""
    return " ".join(str(text or "").strip().lower().split())


def _build_matrix(table: TableData) -> list[list[str]]:
    """Dựng ma trận 2D [row][col] từ danh sách cell của bảng."""
    if table.num_rows <= 0 or table.num_cols <= 0:
        return []
    matrix = [["" for _ in range(table.num_cols)] for _ in range(table.num_rows)]
    for cell in table.cells:
        if 0 <= cell.row < table.num_rows and 0 <= cell.col < table.num_cols:
            matrix[cell.row][cell.col] = (cell.text or "").strip()
    return matrix


def _map_header(header: list[str]) -> dict[str, int]:
    """
    Map từng cột header -> field Keycloak.

    Trả về {field: col_index}. Nếu nhiều cột khớp cùng field, giữ cột đầu tiên.
    """
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


def map_table_to_users(
    table: TableData,
) -> tuple[list[KeycloakUserInput], list[str]]:
    """Map một bảng thành danh sách user + cảnh báo (nếu có)."""
    warnings: list[str] = []
    matrix = _build_matrix(table)
    if len(matrix) < 2:
        return [], warnings

    header = matrix[0]
    col_to_field = _map_header(header)

    if "username" not in col_to_field:
        warnings.append(
            f"Bảng {table.table_index + 1}: không tìm thấy cột 'username' "
            "(kiểm tra KEYCLOAK_HEADER_MAP). Bỏ qua bảng này."
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
            continue  # bỏ dòng rỗng / không có username

        users.append(
            KeycloakUserInput(
                username=username,
                email=_val("email"),
                first_name=_val("first_name"),
                last_name=_val("last_name"),
                password=_val("password"),
            )
        )

    return users, warnings


def map_result_to_users(
    result: OcrResult,
) -> tuple[list[KeycloakUserInput], list[str]]:
    """
    Map toàn bộ kết quả OCR thành danh sách user, loại bỏ username trùng.

    Trả về (users, warnings).
    """
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
                        f"Username trùng lặp trong dữ liệu OCR: '{user.username}' "
                        "(chỉ giữ bản ghi đầu tiên)."
                    )
                    continue
                seen.add(key)
                all_users.append(user)

    return all_users, warnings
