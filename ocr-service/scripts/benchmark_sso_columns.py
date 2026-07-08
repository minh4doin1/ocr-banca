#!/usr/bin/env python3
"""
Benchmark SSO email/role OCR on a PDF file.

Usage:
  python scripts/benchmark_sso_columns.py path/to/sso.pdf
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config import settings
from app.services.user_mapping import map_result_to_users, normalize_roles


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark SSO OCR columns")
    parser.add_argument("pdf", type=Path, help="SSO PDF path")
    parser.add_argument("--job-id", default="bench-sso", help="Temporary job id")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"File not found: {args.pdf}")
        return 1

    from app.services.table_service import process_pdf_job
    from app.models.schemas import JobStatus

    print(f"Model pass1: {settings.vietocr_model}")
    print(f"Model pass2: {settings.vietocr_model_pass2}")
    print(f"DPI: {settings.pdf_dpi}, pass2: {settings.ocr_sso_pass2_enabled}")

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
    with_email = sum(1 for u in users if u.email.endswith("@agribank.com.vn"))
    with_role = sum(1 for u in users if u.roles)
    multi_role = sum(1 for u in users if len(u.roles) > 1)

    print(f"Pages: {result.total_pages}, time: {elapsed:.1f}s")
    print(f"Users: {len(users)}, email OK: {with_email}, role OK: {with_role}, multi-role: {multi_role}")
    print(f"Warnings: {len(warnings)}")
    for u in users[:10]:
        roles = ";".join(u.roles) or normalize_roles(u.role_raw or u.role)
        print(f"  {u.ipcas_code:12} {u.email:35} roles={roles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
