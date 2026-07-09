"""
PDF Text Service — Đọc bảng trực tiếp từ PDF có lớp text (không OCR).

PDF scan (ảnh) vẫn cần OCR. PDF số (Word/Excel in ra) có text nhúng — đọc bằng
pdfplumber chính xác hơn OCR rất nhiều (tương đương nạp Word/Excel gốc).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from app.models.schemas import OcrResult, PageResult
from app.services.excel_service import _matrix_to_table
from app.services.user_mapping import (
    _is_sso_data_first_row,
    _strip_sso_preamble_rows,
    map_table_to_users,
    table_has_user_columns,
)

logger = logging.getLogger(__name__)

_MIN_SSO_ROWS = 1
_MIN_SSO_COLS = 8


def _clean_pdf_cell(text: str) -> str:
    """Chuẩn hóa ô đọc từ PDF text layer."""
    if not text:
        return ""
    t = " ".join(str(text).replace("\n", " ").split())
    if "@" in t or "agribank" in t.lower():
        t = re.sub(r"\s*@\s*", "@", t)
        if "@" in t:
            local, domain = t.split("@", 1)
            local = re.sub(r"\s+", "", local)
            domain = re.sub(r"\s+", "", domain)
            domain = re.sub(
                r"agribankcom\.?vn",
                "agribank.com.vn",
                domain,
                flags=re.I,
            )
            if domain.endswith("comvn"):
                domain = domain[:-5] + "com.vn"
            t = f"{local}@{domain}"
        else:
            t = re.sub(r"\s+", "", t)
    return t.strip()


def _fix_name_cell(name: str) -> str:
    """Bỏ số lạ chen giữa tên do lỗi text layer PDF."""
    t = (name or "").strip()
    if not t:
        return ""
    t = re.sub(r"(?<=\S)\s+\d{1,2}\s+(?=\S)", " ", t)
    t = re.sub(r"^\d+\s+", "", t)
    return " ".join(t.split())


def _normalize_sso_matrix(matrix: list[list[str]]) -> list[list[str]]:
    """Làm sạch ma trận SSO sau khi trích từ PDF."""
    out: list[list[str]] = []
    max_col = max((len(r) for r in matrix), default=0)
    if max_col < _MIN_SSO_COLS:
        return []

    for row in matrix:
        padded = (row + [""] * max_col)[:max_col]
        cells = [_clean_pdf_cell(c) for c in padded]
        if cells[1]:
            cells[1] = _fix_name_cell(cells[1])
        if any(cells):
            out.append(cells)

    out = _strip_sso_preamble_rows(out)
    # Bỏ dòng không có dữ liệu user thật (header lẻ, dòng trống)
    out = [row for row in out if _row_looks_like_sso_data(row)]
    return out


def _row_looks_like_sso_data(row: list[str]) -> bool:
    if _is_sso_data_first_row(row):
        return True
    if len(row) < 8:
        return False
    ipcas = (row[4] or "").strip().upper()
    return ipcas.startswith("QSO")


def _matrix_is_sso_table(matrix: list[list[str]]) -> bool:
    if len(matrix) < _MIN_SSO_ROWS:
        return False
    if not table_has_user_columns(matrix):
        return False
    data_rows = sum(1 for row in matrix if _row_looks_like_sso_data(row))
    return data_rows >= 1


def _extract_tables_from_pdf(pdf_path: Path) -> list[tuple[int, list[list[str]]]]:
    """Trích các bảng SSO từ PDF (page_number 1-based, matrix)."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError(
            "Thiếu pdfplumber. Cài: pip install pdfplumber"
        ) from exc

    found: list[tuple[int, list[list[str]]]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            for raw in page.extract_tables() or []:
                if not raw:
                    continue
                matrix = [
                    [_clean_pdf_cell(c or "") for c in row]
                    for row in raw
                    if row and any(str(c or "").strip() for c in row)
                ]
                matrix = _normalize_sso_matrix(matrix)
                if _matrix_is_sso_table(matrix):
                    found.append((page_idx + 1, matrix))
    return found


def pdf_has_text_layer(pdf_path: str | Path, *, min_chars: int = 200) -> bool:
    """Kiểm tra nhanh PDF có đủ text để đọc trực tiếp."""
    try:
        import pdfplumber
    except ImportError:
        return False

    total = 0
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages[:3]:
            text = page.extract_text() or ""
            total += len(text.strip())
            if total >= min_chars:
                return True
    return total >= min_chars


def import_from_pdf_text(
    pdf_path: str | Path,
    job_id: str,
    filename: str,
) -> OcrResult | None:
    """
    Nạp bảng SSO từ PDF có lớp text. Trả None nếu không trích được bảng hợp lệ.
    """
    pdf_path = Path(pdf_path)
    tables_raw = _extract_tables_from_pdf(pdf_path)
    if not tables_raw:
        return None

    pages: list[PageResult] = []
    for page_num, matrix in tables_raw:
        table = _matrix_to_table(matrix, 0)
        if not table.cells:
            continue
        users, _ = map_table_to_users(table)
        if not users:
            continue
        pages.append(
            PageResult(
                page_number=page_num,
                image_path="",
                tables=[table],
                raw_text="",
            )
        )

    if not pages:
        return None

    total_users = 0
    for p in pages:
        for t in p.tables:
            u, _ = map_table_to_users(t)
            total_users += len(u)

    now = datetime.now()
    logger.info(
        "PDF text import: %s — %d trang, %d user (không OCR)",
        filename,
        len(pages),
        total_users,
    )
    return OcrResult(
        job_id=job_id,
        filename=filename,
        total_pages=len(pages),
        pages=pages,
        is_complete=True,
        created_at=now,
        updated_at=now,
    )


def convert_pdf_to_docx(pdf_path: str | Path, out_path: str | Path) -> Path:
    """Chuyển PDF sang Word (.docx) — tiện sửa tay rồi nạp lại."""
    try:
        from pdf2docx import Converter
    except ImportError as exc:
        raise ValueError(
            "Thiếu pdf2docx. Cài: pip install pdf2docx"
        ) from exc

    pdf_path = Path(pdf_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv = Converter(str(pdf_path))
    try:
        cv.convert(str(out_path))
    finally:
        cv.close()
    return out_path
