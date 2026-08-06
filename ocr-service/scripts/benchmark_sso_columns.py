#!/usr/bin/env python3
"""
Benchmark SSO OCR field accuracy on a PDF file.

Usage:
  python scripts/benchmark_sso_columns.py path/to/sso.pdf
  python scripts/benchmark_sso_columns.py path/to/sso.pdf --json out.json
  python scripts/benchmark_sso_columns.py path/to/sso.pdf \\
      --golden path/to/answer.xlsx --json out.json

Reports per-field pass rates for email, role, CCCD, IPCAS, branch_code.
With --golden, also compares OCR users to Excel ground truth column by column.
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

_GOLDEN_FIELDS = (
    "email",
    "ipcas_code",
    "cccd",
    "phone",
    "branch_code",
    "unit_code",
    "role",
    "name",
)


def _norm(val: str) -> str:
    return re.sub(r"\s+", "", (val or "").strip().lower())


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


def _load_golden_users(xlsx: Path) -> list:
    from app.services.excel_service import import_from_excel
    from app.services.user_mapping import map_result_to_users as _map

    result = import_from_excel(xlsx, "golden", xlsx.name)
    users, _ = _map(result)
    return users


def _user_key(u) -> str:
    email = _norm(u.email)
    if email:
        return f"e:{email}"
    ipcas = _norm(u.ipcas_code)
    if ipcas:
        return f"i:{ipcas}"
    return f"n:{_norm(u.name)}_{_norm(u.cccd)}"


def _field_value(u, field: str) -> str:
    if field == "role":
        roles = u.roles or normalize_roles(u.role or "")
        return ";".join(sorted(roles))
    if field == "name":
        return (u.name or f"{u.last_name} {u.first_name}").strip()
    return str(getattr(u, field, "") or "").strip()


def _compare_to_golden(ocr_users: list, golden_users: list) -> dict:
    golden_by_key = {_user_key(u): u for u in golden_users}
    matched = 0
    per_field: dict[str, dict[str, float | int]] = {
        f: {"ok": 0, "total": 0, "rate": 0.0} for f in _GOLDEN_FIELDS
    }
    misses: list[dict] = []

    for ou in ocr_users:
        key = _user_key(ou)
        gu = golden_by_key.get(key)
        if gu is None:
            # fuzzy: match by CCCD
            cccd = re.sub(r"\D", "", ou.cccd or "")
            for candidate in golden_users:
                if cccd and re.sub(r"\D", "", candidate.cccd or "") == cccd:
                    gu = candidate
                    break
        if gu is None:
            misses.append({"ocr": key, "reason": "no_golden_match"})
            continue
        matched += 1
        for field in _GOLDEN_FIELDS:
            ov = _norm(_field_value(ou, field))
            gv = _norm(_field_value(gu, field))
            per_field[field]["total"] = int(per_field[field]["total"]) + 1
            if field in ("cccd", "phone", "branch_code", "unit_code"):
                ov = re.sub(r"\D", "", ov)
                gv = re.sub(r"\D", "", gv)
            if ov and gv and ov == gv:
                per_field[field]["ok"] = int(per_field[field]["ok"]) + 1
            elif ov == gv:
                per_field[field]["ok"] = int(per_field[field]["ok"]) + 1

    for field, m in per_field.items():
        total = int(m["total"]) or 1
        m["rate"] = round(int(m["ok"]) / total, 4)

    return {
        "golden_users": len(golden_users),
        "ocr_users": len(ocr_users),
        "matched": matched,
        "unmatched": len(misses),
        "fields": per_field,
        "sample_misses": misses[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark SSO OCR columns")
    parser.add_argument("pdf", type=Path, help="SSO PDF path")
    parser.add_argument("--job-id", default="bench-sso", help="Temporary job id")
    parser.add_argument("--json", type=Path, default=None, help="Write metrics JSON")
    parser.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="Excel ground-truth (same SSO export layout) for per-column accuracy",
    )
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
    print("Field shape rates (self-check):")
    for name, m in metrics.items():
        print(f"  {name:12} {m['ok']}/{m['total']} ({m['rate']*100:.1f}%)")
    print(f"Warnings: {len(warnings)}")
    for w in (result.warnings or [])[:10]:
        print(f"  ! {w}")
    for u in users[:10]:
        roles = ";".join(u.roles) or normalize_roles(u.role_raw or u.role)
        print(
            f"  {u.ipcas_code:12} br={u.branch_code:5} {u.email:35} "
            f"cccd={u.cccd:12} roles={roles}"
        )

    golden_cmp = None
    if args.golden:
        if not args.golden.exists():
            print(f"Golden not found: {args.golden}")
            return 4
        golden_users = _load_golden_users(args.golden)
        golden_cmp = _compare_to_golden(users, golden_users)
        print("\nGolden comparison (OCR vs Excel):")
        print(
            f"  matched {golden_cmp['matched']}/{golden_cmp['ocr_users']} "
            f"(golden={golden_cmp['golden_users']}, unmatched={golden_cmp['unmatched']})"
        )
        for name, m in golden_cmp["fields"].items():
            print(f"  {name:12} {m['ok']}/{m['total']} ({m['rate']*100:.1f}%)")

    payload = {
        "pdf": str(args.pdf),
        "elapsed_s": round(elapsed, 2),
        "pages": result.total_pages,
        "users": len(users),
        "multi_role": multi_role,
        "low_conf_cells": low_conf_cells,
        "fields": metrics,
        "warnings": warnings[:50],
        "result_warnings": list(result.warnings or [])[:50],
        "models": {
            "pass1": settings.vietocr_model,
            "pass2": settings.vietocr_model_pass2,
        },
        "golden": golden_cmp,
    }
    if args.json:
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
