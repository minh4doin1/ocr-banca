"""OCR diagnostic on a sample Agribank SSO PDF."""
from __future__ import annotations

import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.services.ocr_service import (
    _is_gibberish_text,
    _sso_layout_from_header_text,
    configure_ocr_device,
    process_page,
)
from app.services.pdf_service import convert_pdf_page, get_page_count
from app.services.user_mapping import map_table_to_users

PDF = Path(
    r"C:\Users\lamanhzuto2k\Downloads\Telegram Desktop\3526_DT_AgribankBanca (1) (1).pdf"
)


def _cell_matrix(cells, max_row=None, max_col=None):
    if not cells:
        return []
    mr = max_row if max_row is not None else max(c.row for c in cells)
    mc = max_col if max_col is not None else max(c.col for c in cells)
    grid = [[""] * (mc + 1) for _ in range(mr + 1)]
    for c in cells:
        if c.row <= mr and c.col <= mc:
            grid[c.row][c.col] = (c.text or "").strip()
    return grid


def analyze_page(page_num: int, job_id: str) -> dict:
    img_path = convert_pdf_page(PDF, job_id, page_num)
    result = process_page(img_path, page_number=page_num, use_gpu=False)
    tables = result.tables or []
    out = {
        "page": page_num,
        "tables": len(tables),
        "table_kinds": [t.table_kind for t in tables],
    }
    if not tables:
        out["error"] = "no_tables"
        return out

    table = tables[0]
    out["num_rows"] = table.num_rows
    out["num_cols"] = table.num_cols
    out["cells"] = len(table.cells)

    header_texts = [c.text for c in table.cells if c.row <= 2]
    out["layout"] = _sso_layout_from_header_text(" ".join(header_texts))

    gib = sum(1 for c in table.cells if _is_gibberish_text(c.text))
    out["gibberish_cells"] = gib

    users, warnings = map_table_to_users(table)
    out["users"] = len(users)
    out["warnings"] = warnings[:5]
    out["sample_users"] = [
        {
            "name": u.name,
            "email": u.email,
            "ipcas": u.ipcas_code,
            "cccd": u.cccd,
            "phone": u.phone,
            "role": u.role,
        }
        for u in users[:3]
    ]

    matrix = _cell_matrix(table.cells)
    out["header_preview"] = matrix[0][:11] if matrix else []
    out["row1_preview"] = matrix[1][:11] if len(matrix) > 1 else []
    out["row2_preview"] = matrix[2][:11] if len(matrix) > 2 else []
    return out


def main() -> None:
    if not PDF.exists():
        print(f"PDF not found: {PDF}")
        sys.exit(1)

    configure_ocr_device(False)
    job_id = f"diag-{uuid.uuid4().hex[:8]}"
    n_pages = get_page_count(PDF)
    print(f"PDF: {PDF.name} ({n_pages} pages)")

    results = []
    for p in range(1, n_pages + 1):
        print(f"OCR page {p}/{n_pages}...", flush=True)
        try:
            results.append(analyze_page(p, job_id))
        except Exception as exc:
            results.append({"page": p, "error": str(exc)})

    out_path = ROOT / "storage" / "test_3526_diag.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")

    total_users = sum(r.get("users", 0) for r in results)
    print(f"Total users mapped: {total_users}")
    for r in results:
        if r.get("users", 0) or r.get("tables"):
            print(
                f"  p{r['page']}: {r.get('num_cols', '?')} cols, "
                f"{r.get('users', 0)} users, gib={r.get('gibberish_cells', 0)}, "
                f"layout={r.get('layout', '?')}"
            )
            if r.get("warnings"):
                print(f"    warn: {r['warnings'][0]}")


if __name__ == "__main__":
    main()
