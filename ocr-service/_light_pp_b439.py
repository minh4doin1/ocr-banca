# -*- coding: utf-8 -*-
"""Light postprocess on b439a3cc_improved.json (role/email/name) + export xlsx."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.models.schemas import CellData, OcrResult, PageResult, TableData
from app.services.excel_service import export_to_excel
from app.services.ocr_service import (
    _format_sso_email,
    _normalize_sso_role_text,
)

JOB = "b439a3cc"
IN_JSON = ROOT / "storage" / "results" / f"{JOB}_improved.json"
OUT_JSON = IN_JSON
OUT_XLSX = ROOT / "storage" / "exports" / f"{JOB}_improved.xlsx"

ROLE_AFTER = ("kế toán", "đại lý", "kiểm", "phê duyệt")
IPCAS_RE = re.compile(r"^[A-Z][A-Z0-9]{3,15}$")
PHONE_RE = re.compile(r"^0\d{8,10}$")


def light_fix_cells(cells: list[dict]) -> list[dict]:
    out = []
    for c in cells:
        text = c.get("text") or ""
        col = c.get("col", 0)
        if col == 1 and text.strip():
            text = re.sub(r"\s+\d{6,}\s*$", "", text).strip()
        elif col == 6 and text.strip():
            text = _format_sso_email(text) or text
        elif col == 8 and text.strip():
            text = _normalize_sso_role_text(text)
        out.append({**c, "text": text})
    return out


def dig(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def check_row(vals: list[str]) -> dict:
    vals = list(vals) + [""] * 10
    vals = vals[:10]
    stt, name, bcode, bname, ipcas, cccd, email, phone, role, unit = vals
    ipcas_ok = bool(IPCAS_RE.match(re.sub(r"\s", "", ipcas.upper())))
    cccd_d = dig(cccd.replace("[?]", ""))
    cccd_ok = len(cccd_d) in (9, 12) and not cccd.startswith("[?]")
    email_l = email.lower()
    local = email_l.split("@", 1)[0] if "@" in email_l else email_l
    email_ok = (
        email_l.endswith("@agribank.com.vn")
        and email_l.count("agribank") == 1
        and "agribank" not in local
        and "wagr" not in local
        and "bank" not in local
    )
    phone_ok = bool(PHONE_RE.match(dig(phone)))
    role_l = role.lower()
    role_ok = any(k in role_l for k in ROLE_AFTER)
    unit_ok = bool(re.fullmatch(r"\d{6,10}", dig(unit)))
    checks = {
        "ipcas": ipcas_ok,
        "cccd": cccd_ok,
        "email": email_ok,
        "phone": phone_ok,
        "role": role_ok,
        "unit": unit_ok,
    }
    return {"ok": all(checks.values()), "checks": checks, "row": " | ".join(vals)}


def evaluate(pages: list[dict]) -> dict:
    per_page = []
    total = good = 0
    bad_rows = []
    for p in pages:
        pn = p.get("page_number", 0)
        page_total = page_good = 0
        for t in p.get("tables") or []:
            cells = t.get("cells") or []
            nr = t.get("num_rows") or (max((c.get("row", 0) for c in cells), default=-1) + 1)
            nc = t.get("num_cols") or (max((c.get("col", 0) for c in cells), default=-1) + 1)
            g = [["" for _ in range(nc)] for _ in range(nr)]
            for c in cells:
                r, col = c.get("row", 0), c.get("col", 0)
                if 0 <= r < nr and 0 <= col < nc:
                    g[r][col] = (c.get("text") or "").strip()
            for i, row in enumerate(g):
                c0 = (row[0] if row else "").strip()
                if "stt" in c0.lower():
                    continue
                if not re.fullmatch(r"\d{1,3}", c0):
                    continue
                if not any(x.strip() for x in row):
                    continue
                q = check_row(row)
                page_total += 1
                total += 1
                if q["ok"]:
                    page_good += 1
                    good += 1
                else:
                    fails = [k for k, v in q["checks"].items() if not v]
                    bad_rows.append(
                        {
                            "page": pn,
                            "row": i,
                            "stt": c0,
                            "name": row[1] if len(row) > 1 else "",
                            "fails": fails,
                            "checks": q["checks"],
                            "vals": {
                                "ipcas": row[4] if len(row) > 4 else "",
                                "cccd": row[5] if len(row) > 5 else "",
                                "email": row[6] if len(row) > 6 else "",
                                "phone": row[7] if len(row) > 7 else "",
                                "role": row[8] if len(row) > 8 else "",
                                "unit": row[9] if len(row) > 9 else "",
                            },
                        }
                    )
        pct = round(100.0 * page_good / page_total, 1) if page_total else 0.0
        per_page.append({"page": pn, "good": page_good, "total": page_total, "pct": pct})
    overall = round(100.0 * good / total, 1) if total else 0.0
    return {
        "overall_pct": overall,
        "good": good,
        "total": total,
        "pages": per_page,
        "bad_rows": bad_rows,
    }


def export_xlsx(result: dict) -> Path | None:
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
    path = export_to_excel(ocr)
    if path and path.resolve() != OUT_XLSX.resolve():
        OUT_XLSX.write_bytes(path.read_bytes())
        return OUT_XLSX
    return path


def main():
    data = json.loads(IN_JSON.read_text(encoding="utf-8"))
    before = evaluate(data.get("pages") or [])
    print("=== BEFORE light postprocess ===")
    print(f"overall {before['overall_pct']}% ({before['good']}/{before['total']})")
    for p in before["pages"]:
        print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
    print(f"  role fails: {sum(1 for b in before['bad_rows'] if 'role' in b['fails'])}")

    for page in data.get("pages") or []:
        for table in page.get("tables") or []:
            table["cells"] = light_fix_cells(table.get("cells") or [])
            if table.get("cells"):
                table["num_rows"] = max(c["row"] for c in table["cells"]) + 1
                table["num_cols"] = max(c["col"] for c in table["cells"]) + 1
    data["improved_via"] = (data.get("improved_via") or "") + "+light_pp_role_email_name"

    after = evaluate(data.get("pages") or [])
    print("\n=== AFTER light postprocess ===")
    print(f"overall {after['overall_pct']}% ({after['good']}/{after['total']})")
    for p in after["pages"]:
        print(f"  page {p['page']}: {p['pct']}% ({p['good']}/{p['total']})")
    role_fails = [b for b in after["bad_rows"] if "role" in b["fails"]]
    print(f"  role fails: {len(role_fails)}")
    print("\n=== STILL-BAD ROWS ===")
    for b in after["bad_rows"]:
        print(
            f"  p{b['page']} stt={b['stt']} name={b['name']!r} fails={b['fails']}"
        )
        for k in b["fails"]:
            print(f"    {k}: {b['vals'].get(k)!r}")

    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT_JSON}")
    xlsx = export_xlsx(data)
    if xlsx:
        print(f"Wrote {xlsx}")


if __name__ == "__main__":
    main()
