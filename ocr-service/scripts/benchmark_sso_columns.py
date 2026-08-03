#!/usr/bin/env python3
"""
Benchmark SSO OCR field accuracy on a PDF file.

Usage:
  python scripts/benchmark_sso_columns.py path/to/sso.pdf
  python scripts/benchmark_sso_columns.py path/to/sso.pdf --json out.json

Reports per-field pass rates for email, role, CCCD, IPCAS, branch_code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings
from app.services.user_mapping import map_result_to_users, normalize_roles


def _field_metrics(users: list) -> dict[str, dict[str, float | int]]:
    total = len(users) or 1
    email_ok = sum(1 for u in users if (u.email or "").endswith("@agribank.com.vn"))
    role_ok = sum(1 for u in users if u.roles)
    cccd_ok = sum(
        1 for u in users if re.fullmatch(r"\d{12}", re.sub(r"\D", "", u.cccd or ""))
    )
    ipcas_ok = sum(
        1
        for u in users
        if re.fullmatch(r"[A-Z][A-Z0-9]{3,15}", re.sub(r"\s", "", (u.ipcas_code or "").upper()))
    )
    branch_ok = sum(
        1 for u in users if re.fullmatch(r"\d{3,5}", re.sub(r"\D", "", u.branch_code or ""))
    )
    phone_ok = sum(
        1
        for u in users
        if re.fullmatch(r"0\d{8,10}", re.sub(r"\D", "", u.phone or ""))
    )

    def _pack(ok: int) -> dict[str, float | int]:
        return {"ok": ok, "total": len(users), "rate": round(ok / total, 4)}

    return {
        "email": _pack(email_ok),
        "role": _pack(role_ok),
        "cccd": _pack(cccd_ok),
        "ipcas": _pack(ipcas_ok),
        "branch_code": _pack(branch_ok),
        "phone": _pack(phone_ok),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark SSO OCR columns")
    parser.add_argument("pdf", type=Path, help="SSO PDF path")
    parser.add_argument("--job-id", default="bench-sso", help="Temporary job id")
    parser.add_argument("--json", type=Path, default=None, help="Write metrics JSON")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"File not found: {args.pdf}")
        return 1

    from app.services.table_service import process_pdf_job
    from app.models.schemas import JobStatus

    print(f"Model pass1: {settings.vietocr_model}")
    print(f"Model pass2: {settings.vietocr_model_pass2}")
    print(f"DPI: {settings.pdf_dpi}, pass2: {settings.ocr_sso_pass2_enabled}")
    print(f"Confidence threshold: {settings.ocr_confidence_threshold}")

    t0 = time.perf_counter()
    job = process_pdf_job(args.pdf, args.job_id, use_gpu=settings.paddle_use_gpu)
    elapsed = time.perf_counter() - t0

    if job.status != JobStatus.COMPLETED:
        print(f"OCR failed: {job.error_message}")
        return 2

    from app.services.table_service import get_result

    result = get_result(args.job_id)
    if result is None:
        print("No OCR result")
        return 3

    users, warnings = map_result_to_users(result)
    metrics = _field_metrics(users)
    multi_role = sum(1 for u in users if len(u.roles) > 1)
    low_conf_cells = 0
    for page in result.pages:
        for table in page.tables:
            for cell in table.cells:
                if cell.confidence < settings.ocr_confidence_threshold:
                    low_conf_cells += 1

    print(f"Pages: {result.total_pages}, time: {elapsed:.1f}s")
    print(f"Users: {len(users)}, multi-role: {multi_role}, low-conf cells: {low_conf_cells}")
    print("Field rates:")
    for name, m in metrics.items():
        print(f"  {name:12} {m['ok']}/{m['total']} ({m['rate']*100:.1f}%)")
    print(f"Warnings: {len(warnings)}")
    for u in users[:10]:
        roles = ";".join(u.roles) or normalize_roles(u.role_raw or u.role)
        print(
            f"  {u.ipcas_code:12} br={u.branch_code:5} {u.email:35} "
            f"cccd={u.cccd:12} roles={roles}"
        )

    payload = {
        "pdf": str(args.pdf),
        "elapsed_s": round(elapsed, 2),
        "pages": result.total_pages,
        "users": len(users),
        "multi_role": multi_role,
        "low_conf_cells": low_conf_cells,
        "fields": metrics,
        "warnings": warnings[:50],
        "models": {
            "pass1": settings.vietocr_model,
            "pass2": settings.vietocr_model_pass2,
        },
    }
    if args.json:
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
