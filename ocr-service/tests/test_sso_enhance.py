"""Tests for SSO accuracy enhancements (row merge, symbol normalize)."""

import numpy as np

from app.config import settings
from app.models.schemas import CellData
from app.services.ocr_service import (
    _adjust_col_lines_to_target,
    _assign_lines_to_grid,
    _detect_and_fix_sso_column_shift,
    _extract_sso_email_local,
    _fill_sso_branch_names,
    _format_sso_email,
    _is_hallucinated_ocr_line,
    _is_sso_footer_garbage_row,
    _is_valid_data_row,
    _join_multiline_ocr_lines,
    _looks_like_sso_cells,
    _merge_fragment_sso_rows,
    _normalize_cell_text,
    _normalize_sso_phone,
    _postprocess_sso_cells,
    _repair_agribank_email,
    _resolve_sso_email_col,
    _row_looks_like_fragment_continuation,
    _split_cell_text_lines,
    _sso_data_column_count,
    _sso_layout_col_count,
    _sso_layout_from_header_text,
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


def test_sso_cell_needs_pass2_branch_low_conf():
    from app.services.ocr_service import _sso_cell_needs_pass2

    assert _sso_cell_needs_pass2("3526", "branch", confidence=0.95) is False
    assert _sso_cell_needs_pass2("3526", "branch", confidence=0.5) is True
    assert _sso_cell_needs_pass2("35", "branch", confidence=0.99) is True


def test_sso_cell_needs_pass2_email_and_role():
    from app.services.ocr_service import _sso_cell_needs_pass2

    assert _sso_cell_needs_pass2("user@agribank.com.vn", "email", confidence=0.9) is False
    assert _sso_cell_needs_pass2("[?] xy", "email", confidence=0.9) is True
    assert _sso_cell_needs_pass2("dai ly vien", "role", confidence=0.99) is True


def test_pick_field_candidate_role_and_branch():
    from app.services.ocr_service import _pick_field_candidate

    role_best = _pick_field_candidate(
        "role",
        [("xyz", 0.99), ("dai ly vien", 0.7)],
    )
    assert role_best[0]

    branch_best = _pick_field_candidate(
        "branch",
        [("35ab", 0.9), ("3526", 0.6)],
    )
    assert "3526" in branch_best[0]


def test_blend_and_normalize_confidence():
    from app.services.ocr_service import (
        _blend_model_confidence,
        _estimate_confidence,
        _normalize_vietocr_prob,
    )

    assert _normalize_vietocr_prob(None) == 0.0
    assert 0.4 <= _normalize_vietocr_prob([0.9, 0.8, 0.7]) <= 1.0
    assert _blend_model_confidence("", 0.9) == 0.0
    assert _blend_model_confidence("Nguyen Van A", 0.91) >= 0.7
    assert _estimate_confidence("") == 0.0


def test_cell_crop_pad_preserves_diacritic_margin():
    from app.services.ocr_service import _cell_crop_pad

    cx1, cy1, cx2, cy2 = _cell_crop_pad([100, 100, 200, 140], (1000, 1000, 3))
    assert cy1 < 100
    assert cy2 > 140
    assert cx1 <= 100
    assert cx2 >= 200

def test_normalize_cccd_text_no_blind_pad():
    from app.services.ocr_service import _normalize_cccd_text

    assert _normalize_cccd_text("08317901156") == "008317901156"
    assert _normalize_cccd_text("083179011568") == "083179011568"


def test_ocr_digit_confusable_replace_O_and_l():
    from app.services.ocr_service import (
        _ocr_digit_confusable_replace,
        _normalize_cccd_text,
    )

    assert _ocr_digit_confusable_replace("O83179O1156l") == "083179011561"
    assert _normalize_cccd_text("O83179011561") == "083179011561"


def test_detect_and_fix_sso_column_shift_corrects_cccd_email():
    from app.services.ocr_service import _detect_and_fix_sso_column_shift

    # Entire data row shifted +1 vs 10-col layout (CCCD at 6 instead of 5).
    cells = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Số CCCD", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
        # row2: data at cols 1..9 (shift +1 from 0..8)
        CellData(row=2, col=1, text="1", confidence=1.0, bbox=[]),
        CellData(row=2, col=2, text="Nguyễn Văn A", confidence=1.0, bbox=[]),
        CellData(row=2, col=3, text="3526", confidence=1.0, bbox=[]),
        CellData(row=2, col=4, text="Chi nhánh HN", confidence=1.0, bbox=[]),
        CellData(row=2, col=5, text="QSO12345", confidence=1.0, bbox=[]),
        CellData(row=2, col=6, text="083179011568", confidence=1.0, bbox=[]),
        CellData(row=2, col=7, text="nguyenvana@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=2, col=8, text="0912345678", confidence=1.0, bbox=[]),
        CellData(row=2, col=9, text="Đại lý viên", confidence=1.0, bbox=[]),
        CellData(row=3, col=1, text="2", confidence=1.0, bbox=[]),
        CellData(row=3, col=2, text="Trần Thị B", confidence=1.0, bbox=[]),
        CellData(row=3, col=3, text="3527", confidence=1.0, bbox=[]),
        CellData(row=3, col=4, text="Chi nhánh HCM", confidence=1.0, bbox=[]),
        CellData(row=3, col=5, text="QSO99999", confidence=1.0, bbox=[]),
        CellData(row=3, col=6, text="012345678901", confidence=1.0, bbox=[]),
        CellData(row=3, col=7, text="tranthib@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=3, col=8, text="0987654321", confidence=1.0, bbox=[]),
        CellData(row=3, col=9, text="Đại lý viên", confidence=1.0, bbox=[]),
    ]
    fixed, warnings = _detect_and_fix_sso_column_shift(cells)
    by = {(c.row, c.col): c.text for c in fixed}
    assert by.get((2, 5)) == "083179011568"
    assert "agribank" in by.get((2, 6), "").lower()
    assert by.get((2, 0)) == "1"
    assert warnings


def test_is_valid_data_row_rejects_garbage_only():
    from app.services.ocr_service import _is_valid_data_row

    cols = {
        0: CellData(row=2, col=0, text="@@@", confidence=0.2, bbox=[]),
        1: CellData(row=2, col=1, text="||||", confidence=0.1, bbox=[]),
        2: CellData(row=2, col=2, text="xxx", confidence=0.1, bbox=[]),
    }
    assert _is_valid_data_row(cols) is False


def test_sso_cell_needs_pass2_cccd_11_digits():
    from app.services.ocr_service import _sso_cell_needs_pass2

    assert _sso_cell_needs_pass2("08317901156", "cccd", confidence=0.99) is True
    assert _sso_cell_needs_pass2("083179011568", "cccd", confidence=0.99) is False


def test_adjust_col_lines_template_still_10_cols():
    from app.services.ocr_service import (
        _adjust_col_lines_to_target,
        _sso_data_column_count,
    )

    col_lines = [0, 50, 100, 150, 200, 250, 300, 350, 400, 500]
    adjusted = _adjust_col_lines_to_target(col_lines, 10)
    assert _sso_data_column_count(adjusted) == 10


def test_adjust_col_lines_drops_spurious_11th_boundary():
    """b439 page2: 11 detected cols — drop false line near right edge, keep IPCAS/CCCD split."""
    # From OpenCV on page_002 (extra boundary at 2763)
    col_lines = [69, 218, 651, 771, 1002, 1245, 1451, 2068, 2263, 2577, 2763, 2895]
    assert _sso_data_column_count(col_lines) == 11
    adjusted = _adjust_col_lines_to_target(col_lines, 10)
    assert _sso_data_column_count(adjusted) == 10
    # Spurious 2763 should be gone; real mid boundaries kept
    assert 2763 not in adjusted
    assert 1245 in adjusted and 1451 in adjusted
    assert adjusted[0] == 69 and adjusted[-1] == 2895


def test_format_sso_email_repairs_q_and_glued_domain():
    assert (
        _format_sso_email("thuyhothithanhagribank.com-vn@agribank.com.vn")
        == "thuyhothithanh@agribank.com.vn"
    )
    assert (
        _format_sso_email("lienphamphuongq@agribank.com.vn")
        == "lienphamphuong@agribank.com.vn"
    )
    assert (
        _format_sso_email("thuydinhthi5qagribankcomvn@agribank.com.vn")
        == "thuydinhthi5@agribank.com.vn"
    )
    assert (
        _format_sso_email("hienbuithanh 00agribank com vn")
        == "hienbuithanh@agribank.com.vn"
    )
    assert (
        _format_sso_email("loanlethikim1qagribankcom.vn@agribank.com.vn")
        == "loanlethikim1@agribank.com.vn"
    )
    assert (
        _format_sso_email("thuyhothithanhagribankcomvn@agribank.com.vn")
        == "thuyhothithanh@agribank.com.vn"
    )
    assert "agribank" not in _extract_sso_email_local(
        "thuydinhthi5qagribankcomvn@agribank.com.vn"
    )
    # Never double-domain
    assert _format_sso_email("useragribank.com.vn@agribank.com.vn").count(
        "@agribank.com.vn"
    ) == 1


def test_normalize_sso_phone_leading_zero_and_strip_domain():
    assert _normalize_sso_phone("338250999") == "0338250999"
    assert _normalize_sso_phone("911436988@agribank.com.vn") == "0911436988"
    assert _normalize_sso_phone("098939779") == "098939779"
    # Stamp prefix + real mobile glued by OCR
    assert _normalize_sso_phone("03010000001990983851259") == "0983851259"
    assert _normalize_sso_phone("8495833333") == "0849583333"
    assert _normalize_cell_text(
        "911436988@agribank.com.vn", col=7, phone_col=7, email_col=6
    ) == "0911436988"


def test_normalize_cccd_accepts_9_digit_cmnd():
    from app.services.ocr_service import _normalize_cccd_text, _sso_cell_needs_pass2

    assert _normalize_cccd_text("113382382") == "113382382"
    assert not _normalize_cccd_text("113382382").startswith("[?]")
    assert _sso_cell_needs_pass2("113382382", "cccd", confidence=0.99) is False
    assert _sso_cell_needs_pass2("083179011568", "cccd", confidence=0.99) is False
    assert _sso_cell_needs_pass2("08317901156", "cccd", confidence=0.99) is True


def test_detect_left_shift_from_branch_name_page2_pattern():
    """Pages 2–4: col3=IPCAS, col4=CCCD, col6=phone@domain, col7=role → shift +1 from col3."""
    header = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=3, text="Tên chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Số CCCD", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
    ]
    rows = []
    samples = [
        ("8", "Bùi Thanh Hiền", "3001", "HLSHIEN", "113121959",
         "hienbuithanh 00agribank com vn", "911436988@agribank.com.vn",
         "Kế toán viên", "17204007", "1"),
        ("9", "Nguyễn Hiền Thảo", "3001", "HLSNTHAO", "113229663",
         "thaonguyenhien agribank com vn", "974439810@agribank.com.vn",
         "Kế toán viên", "17204007", "1"),
        ("10", "Nguyễn Thị Thu Trang", "3001", "HLSNTTR", "112147782",
         "trangnguyenthithu agribank", "984369796@agribank.com.vn",
         "Kế toán viên", "17204007", "1"),
    ]
    for i, s in enumerate(samples):
        r = 2 + i
        for col, val in enumerate(s):
            rows.append(CellData(row=r, col=col, text=val, confidence=1.0, bbox=[]))
    fixed, warnings = _detect_and_fix_sso_column_shift(header + rows)
    by = {(c.row, c.col): c.text for c in fixed}
    assert by.get((2, 4)) == "HLSHIEN"  # IPCAS restored
    assert by.get((2, 5)) == "113121959"  # CCCD
    assert "agribank" in by.get((2, 6), "").lower() or "hienbuithanh" in by.get((2, 6), "")
    assert "911436988" in by.get((2, 7), "")
    assert "Kế toán" in by.get((2, 8), "")
    assert by.get((2, 2)) == "3001"  # branch_code untouched
    assert warnings


def test_footer_garbage_rows_rejected():
    assert _is_sso_footer_garbage_row({
        0: CellData(row=5, col=0, text="Yêu cầu khác:", confidence=1.0, bbox=[]),
    })
    assert _is_sso_footer_garbage_row({
        0: CellData(row=5, col=0, text="Người liên hệ: Lưu", confidence=1.0, bbox=[]),
    })
    assert _is_sso_footer_garbage_row({
        0: CellData(row=5, col=0, text="LẬP", confidence=1.0, bbox=[]),
        1: CellData(row=5, col=1, text="PHIẾU", confidence=1.0, bbox=[]),
        5: CellData(row=5, col=5, text="KIỂM SOÁT", confidence=1.0, bbox=[]),
    })
    assert _is_sso_footer_garbage_row({
        0: CellData(row=5, col=0, text="(Ký, ghi rõ họ tên)", confidence=1.0, bbox=[]),
    })
    assert _is_valid_data_row({
        0: CellData(row=5, col=0, text="Yêu cầu khác:", confidence=1.0, bbox=[]),
        1: CellData(row=5, col=1, text="abc", confidence=1.0, bbox=[]),
    }) is False


def test_sticky_phone_at_email_role_in_phone_remaps():
    cells = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
        CellData(row=2, col=0, text="8", confidence=1.0, bbox=[]),
        CellData(row=2, col=1, text="Bùi Thanh Hiền", confidence=1.0, bbox=[]),
        CellData(row=2, col=2, text="3001", confidence=1.0, bbox=[]),
        CellData(row=2, col=3, text="Lương Sơn", confidence=1.0, bbox=[]),
        CellData(row=2, col=4, text="HLSHIEN", confidence=1.0, bbox=[]),
        CellData(row=2, col=5, text="113121959 hienbuithanh 00agribank com vn", confidence=1.0, bbox=[]),
        CellData(row=2, col=6, text="911436988@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=2, col=7, text="Kế toán viên", confidence=1.0, bbox=[]),
        CellData(row=2, col=8, text="17204007", confidence=1.0, bbox=[]),
    ]
    out = _postprocess_sso_cells(cells)
    by = {(c.row, c.col): c.text for c in out}
    data_rows = sorted({c.row for c in out})
    r = data_rows[0]
    phone = by.get((r, 7), "")
    role = by.get((r, 8), "")
    email = by.get((r, 6), "")
    cccd = by.get((r, 5), "")
    assert phone.startswith("0") and phone.isdigit(), phone
    assert "Kế toán" in role or "toán" in role.lower() or "toan" in role.lower(), role
    assert "@agribank.com.vn" in email.lower(), email
    assert email.lower().count("agribank.com.vn") == 1
    assert cccd == "113121959" or cccd.replace("[?]", "") == "113121959"


def test_branch_name_forward_fill_from_map_and_page1():
    """Empty/IPCAS-looking col3 filled from branch_code map (incl. shared across pages)."""
    shared: dict[str, str] = {}
    page1 = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=3, text="Tên chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Số CCCD", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
        CellData(row=1, col=0, text="1", confidence=1.0, bbox=[]),
        CellData(row=1, col=1, text="Phạm Phương Liên", confidence=1.0, bbox=[]),
        CellData(row=1, col=2, text="3000", confidence=1.0, bbox=[]),
        CellData(row=1, col=3, text="Hội Sở", confidence=1.0, bbox=[]),
        CellData(row=1, col=4, text="HBIPLIEN", confidence=1.0, bbox=[]),
        CellData(row=1, col=5, text="113382382", confidence=1.0, bbox=[]),
        CellData(row=1, col=6, text="lienphamphuong@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=1, col=7, text="0338250999", confidence=1.0, bbox=[]),
        CellData(row=1, col=8, text="Kế toán viên", confidence=1.0, bbox=[]),
        CellData(row=1, col=9, text="12704001", confidence=1.0, bbox=[]),
        CellData(row=2, col=0, text="7", confidence=1.0, bbox=[]),
        CellData(row=2, col=1, text="Nguyễn Thị Minh Thúy", confidence=1.0, bbox=[]),
        CellData(row=2, col=2, text="3001", confidence=1.0, bbox=[]),
        CellData(row=2, col=3, text="Lương Sơn", confidence=1.0, bbox=[]),
        CellData(row=2, col=4, text="HLSNTHUY", confidence=1.0, bbox=[]),
        CellData(row=2, col=5, text="113119679", confidence=1.0, bbox=[]),
        CellData(row=2, col=6, text="thuynguyenthiminh@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=2, col=7, text="0915115147", confidence=1.0, bbox=[]),
        CellData(row=2, col=8, text="Kế toán viên", confidence=1.0, bbox=[]),
        CellData(row=2, col=9, text="17204007", confidence=1.0, bbox=[]),
    ]
    out1 = _postprocess_sso_cells(page1, branch_map=shared)
    assert shared.get("3000") == "Hội Sở"
    assert shared.get("3001") == "Lương Sơn"

    # Page2-style: after shift, col3 empty / missing; IPCAS in col4
    page2 = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=3, text="Tên chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Số CCCD", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
        CellData(row=2, col=0, text="8", confidence=1.0, bbox=[]),
        CellData(row=2, col=1, text="Bùi Thanh Hiền", confidence=1.0, bbox=[]),
        CellData(row=2, col=2, text="3001", confidence=1.0, bbox=[]),
        CellData(row=2, col=4, text="HLSHIEN", confidence=1.0, bbox=[]),
        CellData(row=2, col=5, text="113121959", confidence=1.0, bbox=[]),
        CellData(row=2, col=6, text="hienbuithanh@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=2, col=7, text="0911436988", confidence=1.0, bbox=[]),
        CellData(row=2, col=8, text="Kế toán viên", confidence=1.0, bbox=[]),
        CellData(row=2, col=9, text="17204007", confidence=1.0, bbox=[]),
        CellData(row=3, col=0, text="9", confidence=1.0, bbox=[]),
        CellData(row=3, col=1, text="Nguyễn Hiền Thảo", confidence=1.0, bbox=[]),
        CellData(row=3, col=2, text="3001", confidence=1.0, bbox=[]),
        CellData(row=3, col=3, text="HLNTHAO", confidence=1.0, bbox=[]),  # IPCAS-looking
        CellData(row=3, col=4, text="HLSNTHAO", confidence=1.0, bbox=[]),
        CellData(row=3, col=5, text="113229663", confidence=1.0, bbox=[]),
        CellData(row=3, col=6, text="thaonguyenhien@agribank.com.vn", confidence=1.0, bbox=[]),
        CellData(row=3, col=7, text="0974439810", confidence=1.0, bbox=[]),
        CellData(row=3, col=8, text="Kế toán viên", confidence=1.0, bbox=[]),
        CellData(row=3, col=9, text="17204007", confidence=1.0, bbox=[]),
    ]
    out2 = _postprocess_sso_cells(page2, branch_map=shared)
    by = {(c.row, c.col): c.text for c in out2}
    rows = sorted({c.row for c in out2})
    assert len(rows) >= 2
    for r in rows:
        assert by.get((r, 3)) == "Lương Sơn", (r, by.get((r, 3)), by)

    # Helper alone: insert missing col3 when shared map provided
    sparse = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=3, text="Tên chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=9, text="Mã liên ngân hàng", confidence=1.0, bbox=[]),
        CellData(row=1, col=2, text="3000", confidence=1.0, bbox=[]),
        CellData(row=1, col=4, text="HBIPLIEN", confidence=1.0, bbox=[]),
    ]
    filled = _fill_sso_branch_names(sparse, {"3000": "Hội Sở"})
    assert any(c.row == 1 and c.col == 3 and c.text == "Hội Sở" for c in filled)


def test_footer_rejects_pho_giam_doc_signature():
    assert _is_sso_footer_garbage_row({
        0: CellData(row=7, col=0, text="Hồ T", confidence=1.0, bbox=[]),
        8: CellData(row=7, col=8, text="NGÂN HÀNG NÔNG NGHIỆP", confidence=1.0, bbox=[]),
        9: CellData(row=7, col=9, text="PHÓ GIÁM ĐỐC", confidence=1.0, bbox=[]),
    })


def test_normalize_sso_role_text_ocr_variants():
    from app.services.ocr_service import _normalize_sso_role_text

    assert _normalize_sso_role_text("Kê toán viện") == "Kế toán viên"
    assert _normalize_sso_role_text("Ké toán viện") == "Kế toán viên"
    assert _normalize_sso_role_text("Kê toán viên") == "Kế toán viên"
    assert _normalize_sso_role_text("ke toan vien") == "Kế toán viên"
    assert _normalize_sso_role_text("dai ly vien") == "Đại lý viên"
    assert _normalize_sso_role_text("Kiểm soát viên") == "Kiểm soát viên"
    assert _normalize_sso_role_text("Phê duyệt viên") == "Phê duyệt viên"


def test_format_sso_email_strips_wagr1bank_glued_domain():
    assert (
        _format_sso_email("huyennguyenthuwagr1bank.com.vn@agribank.com.vn")
        == "huyennguyenthu@agribank.com.vn"
    )
    assert (
        _format_sso_email("userwagr1bank.com.vn@agribank.com.vn")
        == "user@agribank.com.vn"
    )
    assert "agribank" not in _extract_sso_email_local(
        "huyennguyenthuwagr1bank.com.vn@agribank.com.vn"
    )
    assert "wagr" not in _extract_sso_email_local(
        "huyennguyenthuwagr1bank.com.vn@agribank.com.vn"
    )


def test_normalize_cell_text_strips_name_trailing_digits():
    assert (
        _normalize_cell_text("Ngô Hông Hoa 030100000000020", col=1)
        == "Ngô Hông Hoa"
    )
    assert (
        _normalize_cell_text("Đô Phương Tùng 03010000000039", col=1)
        == "Đô Phương Tùng"
    )
    # short STT-like trailing digits left to other logic; 6+ only
    assert _normalize_cell_text("Nguyễn Văn A 12", col=1) == "Nguyễn Văn A 12"


def test_normalize_cell_text_role_col():
    assert (
        _normalize_cell_text("Kê toán viện", col=8, role_col=8)
        == "Kế toán viên"
    )


def test_postprocess_normalizes_role_and_name_junk():
    cells = [
        CellData(row=0, col=0, text="STT", confidence=1.0, bbox=[]),
        CellData(row=0, col=2, text="Mã chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=3, text="Tên chi nhánh", confidence=1.0, bbox=[]),
        CellData(row=0, col=5, text="Số CCCD", confidence=1.0, bbox=[]),
        CellData(row=0, col=6, text="Email", confidence=1.0, bbox=[]),
        CellData(row=1, col=0, text="1", confidence=1.0, bbox=[]),
        CellData(row=1, col=1, text="Ngô Hông Hoa 030100000000020", confidence=1.0, bbox=[]),
        CellData(row=1, col=2, text="3000", confidence=1.0, bbox=[]),
        CellData(row=1, col=3, text="Hội Sở", confidence=1.0, bbox=[]),
        CellData(row=1, col=4, text="HBIPLIEN", confidence=1.0, bbox=[]),
        CellData(row=1, col=5, text="113382382", confidence=1.0, bbox=[]),
        CellData(
            row=1,
            col=6,
            text="huyennguyenthuwagr1bank.com.vn@agribank.com.vn",
            confidence=1.0,
            bbox=[],
        ),
        CellData(row=1, col=7, text="0338250999", confidence=1.0, bbox=[]),
        CellData(row=1, col=8, text="Kê toán viện", confidence=1.0, bbox=[]),
        CellData(row=1, col=9, text="12704001", confidence=1.0, bbox=[]),
    ]
    out = _postprocess_sso_cells(cells)
    by = {(c.row, c.col): c.text for c in out}
    r = sorted({c.row for c in out})[0]
    assert by.get((r, 1)) == "Ngô Hông Hoa"
    assert by.get((r, 6)) == "huyennguyenthu@agribank.com.vn"
    assert by.get((r, 8)) == "Kế toán viên"
