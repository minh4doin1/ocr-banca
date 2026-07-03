"""
Excel Service — Export/import OCR results as Excel files.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.config import settings
from app.models.schemas import CellData, OcrResult, PageResult, TableData

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


def import_from_excel(
    excel_path: str | Path,
    job_id: str,
    filename: str,
) -> OcrResult:
    """
    Import data from Excel into OcrResult structure.

    Supported formats:
    - Any sheet with a plain rectangular table (non-empty used range)
    - Exported OCR workbook format where each table starts with:
      "Bảng X — ...", followed by the table grid, with blank rows between tables
    """
    excel_path = Path(excel_path)
    wb = load_workbook(filename=str(excel_path), data_only=True)
    pages: list[PageResult] = []

    page_number = 1
    for ws in wb.worksheets:
        if ws.title.strip().lower() == "chú thích":
            continue

        tables = _extract_tables_from_sheet(ws)
        if not tables:
            continue

        pages.append(
            PageResult(
                page_number=page_number,
                image_path="",
                tables=tables,
                raw_text="",
            )
        )
        page_number += 1

    if not pages:
        raise ValueError("Không tìm thấy dữ liệu bảng trong file Excel")

    now = datetime.now()
    return OcrResult(
        job_id=job_id,
        filename=filename,
        total_pages=len(pages),
        pages=pages,
        is_complete=True,
        created_at=now,
        updated_at=now,
    )


def _extract_tables_from_sheet(ws) -> list[TableData]:
    """Extract one or many tables from a worksheet."""
    title_rows = _find_table_title_rows(ws)
    tables: list[TableData] = []

    if title_rows:
        for idx, title_row in enumerate(title_rows):
            start_row = title_row + 1
            end_row = (
                title_rows[idx + 1] - 2
                if idx + 1 < len(title_rows)
                else ws.max_row
            )
            matrix = _read_matrix(ws, start_row, end_row)
            if not matrix:
                continue
            tables.append(_matrix_to_table(matrix, len(tables)))
        return tables

    # Fallback: parse whole used range as one table
    matrix = _read_matrix(ws, 1, ws.max_row)
    if matrix:
        tables.append(_matrix_to_table(matrix, 0))
    return tables


def _find_table_title_rows(ws) -> list[int]:
    """Find rows that look like exported table titles: 'Bảng X — ...'."""
    import re

    rows: list[int] = []
    for r in range(1, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if isinstance(v, str) and re.match(r"^\s*Bảng\s+\d+", v.strip(), re.IGNORECASE):
            rows.append(r)
    return rows


def _read_matrix(ws, start_row: int, end_row: int) -> list[list[str]]:
    """Read non-empty rectangular matrix from row range."""
    rows: list[list[str]] = []
    max_col = 0

    for r in range(start_row, end_row + 1):
        vals: list[str] = []
        row_non_empty = False
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            txt = "" if v is None else str(v).strip()
            vals.append(txt)
            if txt:
                row_non_empty = True
                max_col = max(max_col, c)
        if row_non_empty:
            rows.append(vals)

    if not rows or max_col == 0:
        return []

    matrix = [r[:max_col] for r in rows]
    return matrix


def _matrix_to_table(matrix: list[list[str]], table_index: int) -> TableData:
    """Convert 2D string matrix to TableData."""
    num_rows = len(matrix)
    num_cols = max((len(r) for r in matrix), default=0)
    cells: list[CellData] = []

    for r, row_vals in enumerate(matrix):
        padded = row_vals + [""] * (num_cols - len(row_vals))
        for c, txt in enumerate(padded):
            cells.append(
                CellData(
                    row=r,
                    col=c,
                    text=txt,
                    confidence=1.0,
                    bbox=[],
                )
            )

    return TableData(
        table_index=table_index,
        num_rows=num_rows,
        num_cols=num_cols,
        cells=cells,
        html="",
    )


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
