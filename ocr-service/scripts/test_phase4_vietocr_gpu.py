"""Benchmark + accuracy test Phase 4 (VietOCR GPU subprocess vs CPU)."""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.services.ocr_service import (
    _is_gibberish_text,
    configure_ocr_device,
    load_page_image,
    prepare_page_draft,
    recognize_page_draft,
)
from app.services.pdf_service import convert_pdf_page
from app.services.vietocr_gpu_client import (
    get_vietocr_gpu_client,
    shutdown_vietocr_gpu_client,
    warmup_vietocr_gpu_worker,
)

PDF = Path(r"C:\Users\lamanhzuto2k\Downloads\Telegram Desktop\3500_Mau 01 SSO.pdf")


def main() -> None:
    if not PDF.exists():
        print(f"PDF not found: {PDF}")
        sys.exit(1)

    configure_ocr_device(settings.paddle_use_gpu)
    gpu_ok = warmup_vietocr_gpu_worker()
    client = get_vietocr_gpu_client(auto_start=False)
    device = client.device if client else "cpu-fallback"
    print(f"vietocr_gpu_subprocess={settings.vietocr_gpu_subprocess} ready={gpu_ok} device={device}")

    job = f"bench-{uuid.uuid4().hex[:8]}"
    img_path = convert_pdf_page(PDF, job, 2)
    img = load_page_image(img_path)
    draft = prepare_page_draft(img, 2)
    if draft is None:
        print("FAIL: no grid draft")
        sys.exit(2)

    t0 = time.perf_counter()
    table = recognize_page_draft(draft)
    elapsed = time.perf_counter() - t0
    if table is None:
        print("FAIL: recognize None")
        sys.exit(3)

    # Verify GPU path used (no in-process CPU VietOCR loaded)
    from app.services import ocr_service

    used_cpu_fallback = ocr_service._vietocr_predictor is not None

    rows = sorted({c.row for c in table.cells})
    junk = [c.text for c in table.cells if "CONTRA" in c.text.upper()]
    gib_rows = [
        r
        for r in rows
        if any(_is_gibberish_text(c.text) for c in table.cells if c.row == r)
    ]

    out = {
        "device": device,
        "used_cpu_vietocr_fallback": used_cpu_fallback,
        "elapsed_sec": round(elapsed, 2),
        "rows": len(rows),
        "cols": table.num_cols,
        "cells": len(table.cells),
        "junk_contra": junk,
        "gibberish_rows": gib_rows,
    }
    out_path = ROOT / "storage" / "test_page2_phase4.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(
        f"time={elapsed:.1f}s rows={len(rows)} cols={table.num_cols} "
        f"junk={len(junk)} gib_rows={len(gib_rows)} cpu_fallback={used_cpu_fallback}"
    )
    shutdown_vietocr_gpu_client()
    ok = len(junk) == 0 and len(rows) <= 22 and gpu_ok and not used_cpu_fallback
    print("PASS" if ok else "CHECK")
    sys.exit(0 if ok else 4)


if __name__ == "__main__":
    main()
