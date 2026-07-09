"""Tests for SSO accuracy enhancements (row merge, symbol normalize)."""

import numpy as np

from app.config import settings
from app.models.schemas import CellData
from app.services.ocr_service import (
    _adjust_col_lines_to_target,
    _assign_lines_to_grid,
    _sso_layout_from_header_text,
    _extract_sso_email_local,
    _format_sso_email,
    _is_hallucinated_ocr_line,
    _join_multiline_ocr_lines,
    _looks_like_sso_cells,
    _merge_fragment_sso_rows,
    _normalize_cell_text,
    _repair_agribank_email,
    _resolve_sso_email_col,
    _row_looks_like_fragment_continuation,
    _split_cell_text_lines,
    _sso_data_column_count,
    _sso_layout_col_count,
    _strip_leading_english_hallucination,
)


def test_split_cell_text_lines_two_bands():
    """Projection split yields 2 line crops for a tall two-line cell."""
    crop = np.full((60, 120, 3), 255, dtype=np.uint8)
    crop[10:22, 20:100] = 0
    crop[34:46, 20:100] = 0
    lines = _split_cell_text_lines(crop)
    assert len(lines) == 2


def test_does_not_merge_rows_with_stt():
    upper = {
        0: CellData(row=0, col=0, text="2", confidence=0.9, bbox=[]),
        1: CellData(row=0, col=1, text="Trương Thị", confidence=0.9, bbox=[]),
    }
    lower = {
        0: CellData(row=1, col=0, text="3", confidence=0.9, bbox=[]),
        1: CellData(row=1, col=1, text="Ngô Thị", confidence=0.9, bbox=[]),
    }
    assert not _row_looks_like_fragment_continuation(upper, lower)


def test_does_not_merge_full_data_row():
    upper = {
        0: CellData(row=0, col=0, text="2", confidence=0.9, bbox=[]),
    }
    lower = {
        1: CellData(row=1, col=1, text="LANLUONG", confidence=0.9, bbox=[]),
        3: CellData(row=1, col=3, text="LANLUONG", confidence=0.9, bbox=[]),
        4: CellData(row=1, col=4, text="083179011564", confidence=0.9, bbox=[]),
        5: CellData(row=1, col=5, text="phuong@ag", confidence=0.9, bbox=[]),
    }
    assert not _row_looks_like_fragment_continuation(upper, lower)


def test_format_sso_email_fixed_domain():
    assert (
        _format_sso_email("luongnguyenthiphu@ag")
        == "luongnguyenthiphu@agribank.com.vn"
    )
    assert (
        _format_sso_email("luongnguyenthiphu ag@ag@agribank.com.vn")
        == "luongnguyenthiphu@agribank.com.vn"
    )
    assert _extract_sso_email_local("user@ag ribank.com.vn") == "user"


def test_normalize_cell_text_agribank_email():
    email_col = 5
    assert (
        _normalize_cell_text(
            "luongnguyenthiphu@ag ribank.com.vn",
            col=email_col,
            email_col=email_col,
        )
        == "luongnguyenthiphu@agribank.com.vn"
    )
    assert (
        _normalize_cell_text(
            "luongnguyenthiphu ag@ag@ag@ag@agribank.com.vn",
            col=email_col,
            email_col=email_col,
        )
        == "luongnguyenthiphu@agribank.com.vn"
    )
    assert _normalize_cell_text("KT 8 NQ") == "KT&NQ"
    assert _normalize_cell_text("KT8NQ") == "KT&NQ"
    assert _normalize_cell_text("KTÁNQ") == "KT&NQ"


def test_normalize_cell_text_department_not_email():
    """Phòng/Đơn vị must not be forced into email format."""
    assert _normalize_cell_text("6900 Hội sở", col=2, email_col=5) == "6900 Hội sở"
    assert (
        _normalize_cell_text("Chi nhánh Agribank Hà Nội", col=2, email_col=5)
        == "Chi nhánh Agribank Hà Nội"
    )


def test_normalize_cell_text_email_column():
    assert (
        _normalize_cell_text("luongnguyenthiphu", col=5, email_col=5)
        == "luongnguyenthiphu@agribank.com.vn"
    )


def test_join_multiline_email():
    joined = _join_multiline_ocr_lines(
        ["luongnguyenthiphu@ag", "ribank.com.vn"]
    )
    assert joined == "luongnguyenthiphu@agribank.com.vn"
    assert (
        _normalize_cell_text(joined) == "luongnguyenthiphu@agribank.com.vn"
    )


def test_join_multiline_name():
    joined = _join_multiline_ocr_lines(["Nguyễn Thị", "Phú Lương"])
    assert joined == "Nguyễn Thị Phú Lương"


def test_strip_english_hallucination_before_vietnamese_name():
    assert (
        _strip_leading_english_hallucination("Concrementation Trịnh Lan Anh")
        == "Trịnh Lan Anh"
    )
    assert (
        _strip_leading_english_hallucination("Lateralization Lê Thị Thủy")
        == "Lê Thị Thủy"
    )
    assert (
        _strip_leading_english_hallucination(
            "Incontercententalized Đàm Văn Đồng"
        )
        == "Đàm Văn Đồng"
    )
    assert (
        _strip_leading_english_hallucination("Nguyễn Thị Phú Lương")
        == "Nguyễn Thị Phú Lương"
    )


def test_join_multiline_skips_hallucinated_band():
    joined = _join_multiline_ocr_lines(
        ["Concrementation", "Trịnh Lan Anh"]
    )
    assert joined == "Trịnh Lan Anh"
    assert _is_hallucinated_ocr_line("Concrementation")
    assert not _is_hallucinated_ocr_line("Trịnh Lan Anh")


def test_repair_agribank_email_collapses_at_ag():
    assert (
        _repair_agribank_email("user ag@ag@ag@ag@agribank.com.vn")
        == "user@agribank.com.vn"
    )


def test_assign_lines_to_grid_wraps_email_second_line():
    row_anchors = [50.0, 120.0]
    lines = [
        {"text": "1", "col": 0, "cy": 50, "y1": 40, "y2": 60, "x1": 0, "x2": 10},
        {
            "text": "luongnguyenthiphu@ag",
            "col": 5,
            "cy": 55,
            "y1": 45,
            "y2": 65,
            "x1": 100,
            "x2": 200,
        },
        {
            "text": "ribank.com.vn",
            "col": 5,
            "cy": 72,
            "y1": 66,
            "y2": 78,
            "x1": 100,
            "x2": 200,
        },
    ]
    grid = _assign_lines_to_grid(lines, row_anchors)
    assert (0, 5) in grid
    assert len(grid[(0, 5)]) == 2
    assert (1, 5) not in grid


def test_merge_fragment_sso_rows_name_wrap():
    cells = [
        CellData(row=0, col=0, text="1", confidence=0.9, bbox=[]),
        CellData(row=0, col=1, text="Nguyễn Thị", confidence=0.9, bbox=[]),
        CellData(row=1, col=1, text="Phú Lương", confidence=0.9, bbox=[]),
    ]
    merged = _merge_fragment_sso_rows(cells)
    by_col = {c.col: c.text for c in merged if c.row == 0}
    assert "Phú Lương" in by_col.get(1, "")
    assert by_col.get(1, "").startswith("Nguyễn")

def test_offset_cells_bbox():
    from app.services.ocr_service import _offset_cells_bbox

    cells = [
        CellData(row=0, col=0, text="x", confidence=1.0, bbox=[10, 20, 30, 40]),
    ]
    out = _offset_cells_bbox(cells, dy=100, dx=0)
    assert out[0].bbox == [10, 120, 30, 140]


def test_merge_fragment_sso_rows_email_wrap():
    cells = [
        CellData(row=0, col=0, text="1", confidence=0.9, bbox=[]),
        CellData(row=0, col=1, text="Nguyễn Thị", confidence=0.9, bbox=[]),
        CellData(row=0, col=5, text="luongnguyenthiphu@ag", confidence=0.9, bbox=[]),
        CellData(row=0, col=8, text="Kiể", confidence=0.9, bbox=[]),
        CellData(row=1, col=1, text="Phú Lương", confidence=0.9, bbox=[]),
        CellData(row=1, col=5, text="ribank.com", confidence=0.9, bbox=[]),
        CellData(row=1, col=6, text=".vn", confidence=0.9, bbox=[]),
        CellData(row=1, col=8, text="viên", confidence=0.9, bbox=[]),
    ]
    merged = _merge_fragment_sso_rows(cells)
    rows = {c.row for c in merged}
    assert len(rows) == 1
    by_col = {c.col: c.text for c in merged if c.row == 0}
    assert "ribank.com" in by_col.get(5, "") or "agribank" in by_col.get(5, "")
    assert by_col.get(1, "").startswith("Nguyễn")


def test_row_continuation_detects_email_fragment():
    upper = {
        0: CellData(row=0, col=0, text="1", confidence=0.9, bbox=[]),
        5: CellData(row=0, col=5, text="user@ag", confidence=0.9, bbox=[]),
    }
    lower = {
        5: CellData(row=1, col=5, text="ribank.com", confidence=0.9, bbox=[]),
        6: CellData(row=1, col=6, text=".vn", confidence=0.9, bbox=[]),
    }
    assert _row_looks_like_fragment_continuation(upper, lower)


def test_looks_like_sso_cells_header():
    cells = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=1, text="Họ và tên", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Email", confidence=1.0, bbox=[]),
        CellData(row=0, col=4, text="CCCD", confidence=1.0, bbox=[]),
    ]
    assert _looks_like_sso_cells(cells)


def test_enhance_flags_exist():
    assert settings.ocr_sso_enhance is True
    assert settings.ocr_symbol_normalize is True
    assert settings.ocr_sso_row_merge is True
    assert settings.ocr_cell_multiline is True
    assert settings.ocr_sso_email_fixed_domain is True
    assert settings.ocr_sso_email_domain == "@agribank.com.vn"


def test_sso_layout_detects_new_10_column_template():
    header = "STT Họ và tên Mã chi nhánh Tên Chi nhánh User IPCAS Số CCCD Email tại Agribank"
    assert _sso_layout_from_header_text(header) == 10


def test_sso_layout_detects_old_9_column_template():
    header = "STT Họ và tên Phòng/Đơn vị User IPCAS Số CCCD Email SĐT Phân quyền"
    assert _sso_layout_from_header_text(header) == 9


def test_email_col_uses_layout_not_grid_count():
    """Mẫu 10 cột nhưng lưới chỉ detect 9 cột — email vẫn phải ở cột 6."""
    cells = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email tại Agribank", confidence=1.0, bbox=[]),
    ]
    assert _resolve_sso_email_col(9, cells) == 6
    assert _sso_layout_col_count(cells, 9) == 10


def test_adjust_col_lines_splits_wide_gap_to_10_columns():
    # 9 cột (10 đường kẻ) → cần 11 đường kẻ cho 10 cột
    col_lines = [0, 50, 100, 150, 200, 250, 300, 350, 400, 500]
    assert _sso_data_column_count(col_lines) == 9
    adjusted = _adjust_col_lines_to_target(col_lines, 10)
    assert _sso_data_column_count(adjusted) == 10
