"""
Excel Service — Export OCR results to Excel files.

Creates well-formatted Excel workbooks from OCR table data,
with separate sheets per page and highlighting for low-confidence cells.
"""

from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import settings
from app.models.schemas import OcrResult

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Styles
# ──────────────────────────────────────────────────────────────

HEADER_FILL = PatternFill(
    start_color="1F4E79", end_color="1F4E79", fill_type="solid"
)
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")

LOW_CONFIDENCE_FILL = PatternFill(
    start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"
)
LOW_CONFIDENCE_FONT = Font(name="Arial", size=11, color="CC6600")

NORMAL_FONT = Font(name="Arial", size=11)
NORMAL_ALIGNMENT = Alignment(
    horizontal="left", vertical="center", wrap_text=True
)

THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)


def export_to_excel(result: OcrResult) -> Path:
    """
    Export OCR result to an Excel file.

    Creates one sheet per page. Each table on a page is placed
    sequentially with a gap row between tables.

    Low-confidence cells (below threshold) are highlighted in yellow.

    Args:
        result: The OcrResult to export

    Returns:
        Path to the generated Excel file
    """
    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    threshold = settings.ocr_confidence_threshold

    for page in result.pages:
        sheet_name = f"Trang {page.page_number}"
        ws = wb.create_sheet(title=sheet_name)

        current_row = 1

        for table in page.tables:
            if not table.cells:
                continue

            # Write table title
            ws.cell(row=current_row, column=1).value = (
                f"Bảng {table.table_index + 1} — "
                f"{table.num_rows} dòng × {table.num_cols} cột"
            )
            ws.cell(row=current_row, column=1).font = Font(
                name="Arial", size=12, bold=True, color="1F4E79"
            )
            current_row += 1

            # Build a 2D grid from cell data
            grid: dict[tuple[int, int], tuple[str, float]] = {}
            for cell in table.cells:
                grid[(cell.row, cell.col)] = (cell.text, cell.confidence)

            # Write cells
            for row_idx in range(table.num_rows):
                for col_idx in range(table.num_cols):
                    text, confidence = grid.get((row_idx, col_idx), ("", 1.0))

                    excel_row = current_row + row_idx
                    excel_col = col_idx + 1

                    ws_cell = ws.cell(row=excel_row, column=excel_col)
                    ws_cell.value = text
                    ws_cell.alignment = NORMAL_ALIGNMENT
                    ws_cell.border = THIN_BORDER

                    # First row as header
                    if row_idx == 0:
                        ws_cell.fill = HEADER_FILL
                        ws_cell.font = HEADER_FONT
                    elif confidence < threshold:
                        # Highlight low-confidence cells
                        ws_cell.fill = LOW_CONFIDENCE_FILL
                        ws_cell.font = LOW_CONFIDENCE_FONT
                    else:
                        ws_cell.font = NORMAL_FONT

            current_row += table.num_rows + 2  # Gap between tables

        # Auto-adjust column widths
        _auto_adjust_column_widths(ws)

    # Add a legend sheet if there are low-confidence cells
    _add_legend_sheet(wb, threshold)

    # Save
    filename = f"{result.job_id}_{result.filename.rsplit('.', 1)[0]}.xlsx"
    export_file = settings.export_path / filename

    wb.save(str(export_file))
    logger.info("Excel exported: %s", export_file)

    return export_file


def _auto_adjust_column_widths(ws) -> None:
    """Auto-adjust column widths based on content."""
    for col_cells in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            if cell.value:
                # Vietnamese text can be wider
                cell_length = len(str(cell.value))
                max_length = max(max_length, cell_length)
        # Add some padding, cap at 50
        adjusted_width = min(max_length + 4, 50)
        ws.column_dimensions[col_letter].width = max(adjusted_width, 12)


def _add_legend_sheet(wb: Workbook, threshold: float) -> None:
    """Add a legend/info sheet explaining the highlighting."""
    ws = wb.create_sheet(title="Chú thích", index=0)

    ws.cell(row=1, column=1).value = "Chú thích — Kết quả OCR"
    ws.cell(row=1, column=1).font = Font(
        name="Arial", size=14, bold=True, color="1F4E79"
    )

    ws.cell(row=3, column=1).value = "Màu ô"
    ws.cell(row=3, column=2).value = "Ý nghĩa"
    ws.cell(row=3, column=1).font = Font(bold=True)
    ws.cell(row=3, column=2).font = Font(bold=True)

    # Header row example
    ws.cell(row=4, column=1).fill = HEADER_FILL
    ws.cell(row=4, column=1).font = HEADER_FONT
    ws.cell(row=4, column=1).value = "Header"
    ws.cell(row=4, column=2).value = "Dòng tiêu đề bảng"

    # Low confidence example
    ws.cell(row=5, column=1).fill = LOW_CONFIDENCE_FILL
    ws.cell(row=5, column=1).font = LOW_CONFIDENCE_FONT
    ws.cell(row=5, column=1).value = "Cần review"
    ws.cell(row=5, column=2).value = (
        f"Ô có độ tin cậy OCR < {threshold:.0%} — cần kiểm tra lại"
    )

    # Normal example
    ws.cell(row=6, column=1).font = NORMAL_FONT
    ws.cell(row=6, column=1).value = "Bình thường"
    ws.cell(row=6, column=2).value = "Ô đã nhận dạng chính xác"

    ws.cell(row=8, column=1).value = (
        "Lưu ý: Các ô được highlight vàng cần được kiểm tra lại "
        "trước khi sử dụng để tạo lô user."
    )
    ws.cell(row=8, column=1).font = Font(italic=True, color="666666")

    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 60
