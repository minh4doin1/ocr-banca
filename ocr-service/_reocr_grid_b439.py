# -*- coding: utf-8 -*-
"""Full SSO-grid re-OCR for job b439a3cc via OpenCV grid + VietOCR (no Paddle det)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Bypass OneDNN before any paddle import side-effects
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_mkldnn", "0")
os.environ.setdefault("FLAGS_use_mkldnn_int8_bfloat16", "0")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

JOB = "b439a3cc"
IMG_DIR = ROOT / "storage" / "images" / JOB
OLD_JSON = ROOT / "storage" / "results" / f"{JOB}.json"
OUT_JSON = ROOT / "storage" / "results" / f"{JOB}_improved.json"
OUT_XLSX = ROOT / "storage" / "exports" / f"{JOB}_improved.xlsx"

from _reocr_b439 import evaluate_pages, load_old  # noqa: E402


def _table_to_dict(table, *, warnings=None) -> dict:
    cells = table.cells if hasattr(table, "cells") else table.get("cells") or []
    out_cells = []
    for c in cells:
        if hasattr(c, "row"):
            out_cells.append(
                {
                    "row": c.row,
                    "col": c.col,
                    "text": c.text or "",
                    "confidence": float(c.confidence or 0),
                    "bbox": list(c.bbox or []),
                }
            )
        else:
            out_cells.append(c)
    max_r = max((c["row"] for c in out_cells), default=-1) + 1
    max_c = max((c["col"] for c in out_cells), default=-1) + 1
    return {
        "table_index": getattr(table, "table_index", 0) if hasattr(table, "table_index") else table.get("table_index", 0),
        "num_rows": getattr(table, "num_rows", max_r) if hasattr(table, "num_rows") else table.get("num_rows", max_r),
        "num_cols": getattr(table, "num_cols", max_c) if hasattr(table, "num_cols") else table.get("num_cols", max_c),
        "kind": getattr(table, "table_kind", None) or (table.get("kind") if isinstance(table, dict) else None) or "sso_agribank",
        "cells": out_cells,
        "warnings": list(warnings or getattr(table, "warnings", None) or []),
    }


def _sample_rows(pages: list[dict], page_no: int, n: int = 3) -> list[str]:
    for p in pages:
        if p.get("page_number") != page_no:
            continue
        rows = []
        for t in p.get("tables") or []:
            cells = t.get("cells") or []
            nr = t.get("num_rows") or (max((c.get("row", 0) for c in cells), default=-1) + 1)
            nc = t.get("num_cols") or (max((c.get("col", 0) for c in cells), default=-1) + 1)
            g = [["" for _ in range(nc)] for _ in range(nr)]
            for c in cells:
                r, col = c.get("row", 0), c.get("col", 0)
                if 0 <= r < nr and 0 <= col < nc:
                    g[r][col] = (c.get("text") or "").strip()
            for row in g:
                if not any(x.strip() for x in row):
                    continue
                c0 = (row[0] if row else "").strip().lower()
                if "stt" in c0:
                    continue
                rows.append(" | ".join(row[:10]))
                if len(rows) >= n:
                    return rows
        return rows
    return []


def reocr_grid(*, use_gpu: bool, disable_pass2: bool) -> dict:
    import cv2
    from app.config import settings
    from app.services import ocr_service as o
    from app.utils.image_utils import prepare_page_for_ocr

    try:
        import torch  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "torch not installed in this Python. Use ocr-service\\venv:\n"
            "  .\\venv\\Scripts\\python.exe _reocr_grid_b439.py --gpu\n"
            f"({exc})"
        ) from exc

    o._disable_pp_structure("onednn bypass — OpenCV+VietOCR grid reocr")
    if disable_pass2:
        settings.ocr_sso_pass2_enabled = False

    o.configure_ocr_device(use_gpu)
    # Prefer VietOCR GPU worker when available; otherwise in-process CPU torch
    print(
        f"  vietocr_gpu_subprocess={settings.vietocr_gpu_subprocess} "
        f"paddle_use_gpu={settings.paddle_use_gpu}",
        flush=True,
    )

    images = sorted(IMG_DIR.glob("page_*.png"))
    if not images:
        raise SystemExit(f"No images in {IMG_DIR}")

    branch_map: dict[str, str] = {}
    pages: list[dict] = []

    for img_path in images:
        m = re.search(r"(\d+)", img_path.stem)
        pn = int(m.group(1)) if m else len(pages) + 1
        print(f"[grid-reocr] page {pn}: {img_path.name} ...", flush=True)
        t0 = time.time()

        img = cv2.imread(str(img_path))
        if img is None:
            raise RuntimeError(f"Cannot read {img_path}")
        img = prepare_page_for_ocr(img, enhance_lines=True)

        # OpenCV-only path — never call _detect_lines_in_region / PaddleOCR
        draft = o._prepare_sso_grid_draft_opencv(
            img, pn, already_deskewed=True, prefer_cols=10
        )
        table = None
        if draft is not None:
            print(
                f"  grid rows={len(draft.row_lines)-1} cols={len(draft.col_lines)-1} "
                f"top={draft.table_top}",
                flush=True,
            )
            table = o._recognize_sso_grid_draft(draft, branch_map=branch_map)

        if table is None and draft is not None:
            # Manual recognize if draft had too few cells for helper threshold
            cells = o._ocr_table_grid(draft.crop, draft.row_lines, draft.col_lines)
            if cells:
                cells = o._offset_cells_bbox(cells, dy=draft.table_top, dx=0)
                cells = o._postprocess_sso_cells(cells, branch_map=branch_map)
                if cells:
                    max_row = max(c.row for c in cells)
                    max_col = max(c.col for c in cells)
                    from app.models.schemas import TableData

                    table = TableData(
                        table_index=0,
                        num_rows=max_row + 1,
                        num_cols=max_col + 1,
                        cells=cells,
                        html="",
                        table_kind="sso_agribank",
                        warnings=o.consume_sso_postprocess_warnings(),
                    )

        elapsed = time.time() - t0
        if table is None:
            print(f"  FAILED in {elapsed:.1f}s — empty page", flush=True)
            pages.append(
                {
                    "page_number": pn,
                    "image_path": str(img_path),
                    "tables": [],
                    "raw_text": "",
                    "warnings": ["grid reocr produced no table"],
                }
            )
            continue

        td = _table_to_dict(table)
        print(
            f"  done in {elapsed:.1f}s rows={td['num_rows']} cols={td['num_cols']} "
            f"cells={len(td['cells'])} branch_map={len(branch_map)}",
            flush=True,
        )
        pages.append(
            {
                "page_number": pn,
                "image_path": str(img_path),
                "tables": [td],
                "raw_text": "",
                "warnings": td.get("warnings") or [],
            }
        )

    return {
        "job_id": JOB,
        "filename": "1384 KTNQ_0001.pdf",
        "total_pages": len(pages),
        "pages": pages,
        "is_complete": True,
        "improved_via": "opencv_vietocr_grid",
        "branch_map": dict(branch_map),
    }


def _export_excel(result: dict) -> Path | None:
    try:
        from app.models.schemas import CellData, OcrResult, PageResult, TableData
        from app.services.excel_service import export_to_excel

        pages = []
        for p in result["pages"]:
            tables = []
            for t in p.get("tables") or []:
                cells = [
                    CellData(
                        row=c["row"],
                        col=c["col"],
                        text=c.get("text") or "",
                        confidence=float(c.get("confidence") or 0),
                        bbox=c.get("bbox") or [],
                    )
                    for c in (t.get("cells") or [])
                ]
                tables.append(
                    TableData(
                        table_index=t.get("table_index", 0),
                        num_rows=t.get("num_rows") or 0,
                        num_cols=t.get("num_cols") or 0,
                        cells=cells,
                        html="",
                        table_kind="sso_agribank",
                        warnings=t.get("warnings") or [],
                    )
                )
            pages.append(
                PageResult(
                    page_number=p["page_number"],
                    image_path=p.get("image_path") or "",
                    tables=tables,
                    raw_text="",
                    warnings=p.get("warnings") or [],
                )
            )
        ocr = OcrResult(
            job_id=JOB,
            filename=result.get("filename") or f"{JOB}.pdf",
            total_pages=len(pages),
            pages=pages,
            is_complete=True,
        )
        OUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
        path = export_to_excel(ocr)
        # export_to_excel picks its own name under storage/exports — copy/rename if needed
        if path and path.resolve() != OUT_XLSX.resolve():
            OUT_XLSX.write_bytes(path.read_bytes())
            return OUT_XLSX
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"Excel export skipped: {exc}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", action="store_true", default=False)
    ap.add_argument("--cpu", action="store_true", default=False)
    ap.add_argument(
        "--pass2",
        action="store_true",
        default=False,
        help="Keep SSO pass-2 (may call Paddle cell OCR)",
    )
    ap.add_argument("--skip-excel", action="store_true", default=False)
    args = ap.parse_args()

    use_gpu = True
    if args.cpu:
        use_gpu = False
    elif args.gpu:
        use_gpu = True
    else:
        # Auto: prefer GPU when configured
        try:
            from app.config import settings

            use_gpu = bool(settings.paddle_use_gpu or settings.vietocr_gpu_subprocess)
        except Exception:
            use_gpu = True

    old = load_old()
    old_q = evaluate_pages(old.get("pages") or [])
    print("=== BEFORE (old JSON) ===")
    print(f"overall {old_q['overall_pct']}% ({old_q['good']}/{old_q['total']})")
    for p in old_q["pages"]:
        print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")

    print(f"\n=== OPENCV+VIETOCR GRID RE-OCR (gpu={use_gpu}) ===", flush=True)
    result = reocr_grid(use_gpu=use_gpu, disable_pass2=not args.pass2)
    new_q = evaluate_pages(result["pages"])
    print(f"\noverall {new_q['overall_pct']}% ({new_q['good']}/{new_q['total']})")
    for p in new_q["pages"]:
        print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
        for s in p["samples"][:2]:
            print(f"    [{'OK' if s['ok'] else '..'}] {s['row'][:140]}")
            if not s["ok"]:
                print(f"         checks={s['checks']}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")
    print(f"branch_map: {result.get('branch_map')}")

    if not args.skip_excel:
        xlsx = _export_excel(result)
        if xlsx:
            print(f"Wrote {xlsx}")

    print("\n=== BEFORE vs AFTER ===")
    print(f"before: {old_q['overall_pct']}%  after: {new_q['overall_pct']}%")
    for op, np_ in zip(old_q["pages"], new_q["pages"]):
        print(f"  page {op['page']}: {op['pct']}% -> {np_['pct']}%")

    print("\n=== SAMPLE ROWS ===")
    for pn in (1, 2, 5):
        print(f"-- page {pn} --")
        for line in _sample_rows(result["pages"], pn, 3):
            print(" ", line[:160])


if __name__ == "__main__":
    main()
