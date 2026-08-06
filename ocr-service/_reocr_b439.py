# -*- coding: utf-8 -*-
"""Re-OCR / re-postprocess job b439a3cc and report quality metrics."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

JOB = "b439a3cc"
IMG_DIR = ROOT / "storage" / "images" / JOB
OLD_JSON = ROOT / "storage" / "results" / f"{JOB}.json"
OUT_JSON = ROOT / "storage" / "results" / f"{JOB}_improved.json"

IPCAS_RE = re.compile(r"^[A-Z][A-Z0-9]{3,15}$")
PHONE_RE = re.compile(r"^0\d{8,10}$")
ROLE_KEYS = ("kế toán", "ke toan", "ké toan", "kê toán", "toán viên", "toan vien", "đại lý", "dai ly")


def _grid(cells, num_rows, num_cols):
    g = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    for c in cells:
        r, col = c.get("row", 0), c.get("col", 0)
        if 0 <= r < num_rows and 0 <= col < num_cols:
            g[r][col] = (c.get("text") or "").strip()
    return g


def _row_quality(vals: list[str]) -> dict:
    vals = list(vals) + [""] * 10
    vals = vals[:10]
    stt, name, bcode, bname, ipcas, cccd, email, phone, role, unit = vals
    ipcas_ok = bool(IPCAS_RE.match(re.sub(r"\s", "", ipcas.upper())))
    cccd_d = re.sub(r"\D", "", cccd)
    cccd_ok = len(cccd_d) in (9, 12) and not cccd.startswith("[?]")
    email_l = email.lower()
    email_ok = (
        "@" in email_l
        and email_l.endswith("@agribank.com.vn")
        and email_l.count("agribank") == 1
        and "agribank" not in email_l.split("@", 1)[0]
    )
    phone_ok = bool(PHONE_RE.match(re.sub(r"\D", "", phone)))
    role_l = role.lower()
    role_ok = any(k in role_l for k in ROLE_KEYS)
    unit_d = re.sub(r"\D", "", unit)
    unit_ok = bool(re.fullmatch(r"\d{6,10}", unit_d))
    checks = [ipcas_ok, cccd_ok, email_ok, phone_ok, role_ok, unit_ok]
    return {
        "ok": all(checks),
        "checks": {
            "ipcas": ipcas_ok,
            "cccd": cccd_ok,
            "email": email_ok,
            "phone": phone_ok,
            "role": role_ok,
            "unit": unit_ok,
        },
        "row": " | ".join(vals),
    }


def evaluate_pages(pages: list[dict]) -> dict:
    per_page = []
    total = 0
    good = 0
    for p in pages:
        pn = p.get("page_number", 0)
        page_total = 0
        page_good = 0
        samples = []
        for t in p.get("tables") or []:
            cells = t.get("cells") or []
            nr = t.get("num_rows") or (max((c.get("row", 0) for c in cells), default=-1) + 1)
            nc = t.get("num_cols") or (max((c.get("col", 0) for c in cells), default=-1) + 1)
            grid = _grid(cells, nr, nc)
            for i, row in enumerate(grid):
                # skip header-like
                c0 = (row[0] if row else "").strip()
                if "stt" in c0.lower():
                    continue
                if not any(x.strip() for x in row):
                    continue
                q = _row_quality(row)
                page_total += 1
                total += 1
                if q["ok"]:
                    page_good += 1
                    good += 1
                if len(samples) < 3:
                    samples.append({"ok": q["ok"], "checks": q["checks"], "row": q["row"]})
        pct = round(100.0 * page_good / page_total, 1) if page_total else 0.0
        per_page.append(
            {
                "page": pn,
                "good": page_good,
                "total": page_total,
                "pct": pct,
                "samples": samples,
            }
        )
    overall = round(100.0 * good / total, 1) if total else 0.0
    return {"overall_pct": overall, "good": good, "total": total, "pages": per_page}


def load_old():
    return json.loads(OLD_JSON.read_text(encoding="utf-8"))


def cells_from_dict(cells):
    from app.models.schemas import CellData

    out = []
    for c in cells:
        out.append(
            CellData(
                row=c.get("row", 0),
                col=c.get("col", 0),
                text=c.get("text") or "",
                confidence=float(c.get("confidence") or 0),
                bbox=c.get("bbox") or [],
            )
        )
    return out


def postprocess_only(old: dict) -> dict:
    from app.services.ocr_service import _postprocess_sso_cells

    pages = []
    branch_map: dict[str, str] = {}
    for p in old.get("pages") or []:
        tables = []
        for t in p.get("tables") or []:
            cells = cells_from_dict(t.get("cells") or [])
            # Inject synthetic header so layout/sticky stay on 10-col SSO
            header = cells_from_dict(
                [
                    {"row": 0, "col": 0, "text": "STT", "confidence": 1, "bbox": []},
                    {"row": 0, "col": 2, "text": "Mã chi nhánh", "confidence": 1, "bbox": []},
                    {"row": 0, "col": 3, "text": "Tên chi nhánh", "confidence": 1, "bbox": []},
                    {"row": 0, "col": 5, "text": "Số CCCD", "confidence": 1, "bbox": []},
                    {"row": 0, "col": 6, "text": "Email", "confidence": 1, "bbox": []},
                ]
            )
            # Shift data rows down by 1 to make room for header
            shifted = []
            for c in cells:
                shifted.append(
                    type(c)(
                        row=c.row + 1,
                        col=c.col,
                        text=c.text,
                        confidence=c.confidence,
                        bbox=c.bbox,
                    )
                )
            fixed = _postprocess_sso_cells(header + shifted, branch_map=branch_map)
            max_r = max((c.row for c in fixed), default=-1) + 1
            max_c = max((c.col for c in fixed), default=-1) + 1
            tables.append(
                {
                    "table_index": t.get("table_index", 0),
                    "num_rows": max_r,
                    "num_cols": max_c,
                    "kind": t.get("kind") or "sso_agribank",
                    "cells": [
                        {
                            "row": c.row,
                            "col": c.col,
                            "text": c.text,
                            "confidence": c.confidence,
                            "bbox": c.bbox,
                        }
                        for c in fixed
                    ],
                    "warnings": list(getattr(__import__("app.services.ocr_service", fromlist=["_SSO_POSTPROCESS_WARNINGS"]), "_SSO_POSTPROCESS_WARNINGS", []) or []),
                }
            )
        pages.append(
            {
                "page_number": p.get("page_number"),
                "image_path": p.get("image_path"),
                "tables": tables,
                "raw_text": p.get("raw_text") or "",
                "warnings": p.get("warnings") or [],
            }
        )
    return {
        "job_id": JOB,
        "filename": old.get("filename"),
        "total_pages": len(pages),
        "pages": pages,
        "is_complete": True,
        "improved_via": "postprocess_only",
    }


def full_reocr(use_gpu: bool | None) -> dict:
    import cv2
    from app.services.ocr_service import (
        configure_ocr_device,
        process_page,
        prepare_page_draft,
        recognize_page_draft,
        build_page_result_from_table,
        load_page_image,
    )

    images = sorted(IMG_DIR.glob("page_*.png"))
    if not images:
        raise SystemExit(f"No images in {IMG_DIR}")

    if use_gpu is not None:
        configure_ocr_device(use_gpu)

    pages = []
    for img_path in images:
        m = re.search(r"(\d+)", img_path.stem)
        pn = int(m.group(1)) if m else len(pages) + 1
        print(f"[reocr] page {pn}: {img_path.name} ...", flush=True)
        t0 = time.time()
        # Prefer SSO draft pipeline; fall back to process_page
        try:
            img = load_page_image(img_path, enable_preprocessing=True)
            draft = prepare_page_draft(img, pn, enable_preprocessing=False)
            table = recognize_page_draft(draft) if draft else None
            if table is not None:
                pr = build_page_result_from_table(img_path, pn, table)
            else:
                pr = process_page(img_path, pn, enable_preprocessing=True, use_gpu=use_gpu)
        except Exception as exc:
            print(f"  draft failed ({exc}); process_page fallback", flush=True)
            pr = process_page(img_path, pn, enable_preprocessing=True, use_gpu=use_gpu)
        elapsed = time.time() - t0
        print(f"  done in {elapsed:.1f}s tables={len(pr.tables)}", flush=True)
        pages.append(pr.model_dump() if hasattr(pr, "model_dump") else pr.dict())

    return {
        "job_id": JOB,
        "filename": "1384 KTNQ_0001.pdf",
        "total_pages": len(pages),
        "pages": pages,
        "is_complete": True,
        "improved_via": "full_reocr",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=("postprocess", "reocr", "both"),
        default="both",
        help="postprocess=replay fixes on old JSON; reocr=full OCR; both=postprocess then reocr",
    )
    ap.add_argument("--gpu", action="store_true", default=False)
    ap.add_argument("--cpu", action="store_true", default=False)
    args = ap.parse_args()
    use_gpu = True if args.gpu else (False if args.cpu else None)

    old = load_old()
    old_q = evaluate_pages(old.get("pages") or [])
    print("=== BEFORE (old JSON) ===")
    print(f"overall {old_q['overall_pct']}% ({old_q['good']}/{old_q['total']})")
    for p in old_q["pages"]:
        print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")

    result = None
    if args.mode in ("postprocess", "both"):
        print("\n=== POSTPROCESS-ONLY ===", flush=True)
        result = postprocess_only(old)
        pq = evaluate_pages(result["pages"])
        print(f"overall {pq['overall_pct']}% ({pq['good']}/{pq['total']})")
        for p in pq["pages"]:
            print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
            for s in p["samples"][:2]:
                print(f"    [{'OK' if s['ok'] else '..'}] {s['row'][:120]}")
                if not s["ok"]:
                    print(f"         checks={s['checks']}")

    if args.mode in ("reocr", "both"):
        print("\n=== FULL RE-OCR ===", flush=True)
        try:
            result = full_reocr(use_gpu)
            rq = evaluate_pages(result["pages"])
            print(f"overall {rq['overall_pct']}% ({rq['good']}/{rq['total']})")
            for p in rq["pages"]:
                print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
                for s in p["samples"][:2]:
                    print(f"    [{'OK' if s['ok'] else '..'}] {s['row'][:120]}")
                    if not s["ok"]:
                        print(f"         checks={s['checks']}")
        except Exception as exc:
            print(f"FULL RE-OCR failed: {exc}", flush=True)
            print("Falling back to postprocess-only on existing JSON.", flush=True)
            result = postprocess_only(old)
            pq = evaluate_pages(result["pages"])
            print(f"overall {pq['overall_pct']}% ({pq['good']}/{pq['total']})")
            for p in pq["pages"]:
                print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
                for s in p["samples"][:2]:
                    print(f"    [{'OK' if s['ok'] else '..'}] {s['row'][:120]}")

    assert result is not None
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")

    # Compare summary
    new_q = evaluate_pages(result["pages"])
    print("\n=== BEFORE vs AFTER ===")
    print(f"before: {old_q['overall_pct']}%  after: {new_q['overall_pct']}%")
    for op, np_ in zip(old_q["pages"], new_q["pages"]):
        print(f"  page {op['page']}: {op['pct']}% -> {np_['pct']}%")


if __name__ == "__main__":
    main()
