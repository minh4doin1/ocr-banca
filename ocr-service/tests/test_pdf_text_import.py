"""Tests cho pdf_text_service — đọc bảng từ PDF có lớp text."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.pdf_text_service import (
    _clean_pdf_cell,
    _fix_name_cell,
    _matrix_is_sso_table,
    _normalize_sso_matrix,
    import_from_pdf_text,
)


def test_clean_pdf_cell_email_spaces():
    assert _clean_pdf_cell("Tuanphamanh27 @ agrib ank.com.vn") == "Tuanphamanh27@agribank.com.vn"


def test_fix_name_cell_removes_digit_artifact():
    assert _fix_name_cell("Lê Hoài 4 Nam") == "Lê Hoài Nam"


def test_normalize_sso_matrix_strips_preamble():
    matrix = [
        ["STT", "Ho va ten", "Ma chi nhanh", "Ten Chi nhanh", "User IPCAS",
         "So CCCD", "Email tai Agribank", "SDT", "Phan quyen", "Ma lien ngan hang"],
        ["1", "Pham Anh Tuan", "3526", "CN TG", "QSOPTUAN", "038093031725",
         "tuan@agribank.com.vn", "0987654321", "dai ly vien", ""],
    ]
    out = _normalize_sso_matrix(matrix)
    assert _matrix_is_sso_table(out)
    assert out[0][4] == "QSOPTUAN"


def test_import_from_pdf_text_mock(tmp_path):
    fake_matrix = [
        ["1", "Pham Anh Tuan", "3526", "CN TG", "QSOPTUAN", "038093031725",
         "tuan@agribank.com.vn", "0987654321", "dai ly vien", ""],
        ["2", "Le Hoai Nam", "3526", "CN TG", "QSOLHNAM", "038192043300",
         "nam@agribank.com.vn", "0912345678", "ke toan vien", ""],
    ]
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with patch(
        "app.services.pdf_text_service._extract_tables_from_pdf",
        return_value=[(2, fake_matrix)],
    ):
        result = import_from_pdf_text(pdf_path, "job-pdf", "sample.pdf")

    assert result is not None
    assert result.total_pages == 1
    assert result.pages[0].page_number == 2
    assert len(result.pages[0].tables) == 1
    table = result.pages[0].tables[0]
    grid = {(c.row, c.col): c.text for c in table.cells}
    assert grid[(0, 4)] == "QSOPTUAN"
    assert all(c.confidence == 1.0 for c in table.cells)
