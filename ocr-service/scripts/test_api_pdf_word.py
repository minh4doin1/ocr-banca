"""Test API: upload PDF → text import → export Word → re-upload Word."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://localhost:8100"
PDF = Path(
    r"C:\Users\lamanhzuto2k\Downloads\Telegram Desktop\3526_DT_AgribankBanca (1) (1).pdf"
)


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    import uuid

    boundary = uuid.uuid4().hex
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(f"{value}\r\n".encode())
    for name, (fname, data, ctype) in files.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode()
        )
        lines.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        lines.append(data)
        lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def _post_multipart(url: str, fields: dict, files: dict) -> dict:
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main() -> int:
    if not PDF.exists():
        print(f"SKIP: khong tim thay {PDF}")
        return 1

    print("1. Upload PDF...")
    data = _post_multipart(
        f"{API}/api/ocr/upload",
        {"processing_mode": "local", "use_gpu": "false"},
        {"file": (PDF.name, PDF.read_bytes(), "application/pdf")},
    )
    job_id = data["job_id"]
    print(f"   Job: {job_id}")

    print("2. Cho xu ly...")
    for _ in range(90):
        try:
            job = _get(f"{API}/api/ocr/status/{job_id}")
        except urllib.error.HTTPError:
            time.sleep(1)
            continue
        st = job.get("status")
        if st == "completed":
            break
        if st == "failed":
            print(f"   FAIL: {job.get('error_message')}")
            return 1
        time.sleep(1)
    else:
        print("   TIMEOUT")
        return 1

    print("3. Lay ket qua...")
    result = _get(f"{API}/api/ocr/result/{job_id}")
    users = 0
    qso = 0
    for page in result.get("pages", []):
        for table in page.get("tables", []):
            for cell in table.get("cells", []):
                if cell.get("col") == 4 and str(cell.get("text", "")).upper().startswith("QSO"):
                    qso += 1
            users += table.get("num_rows", 0)
    print(f"   {len(result.get('pages', []))} trang, ~{users} dong, {qso} IPCAS QSO*")

    print("4. Export Word...")
    out_docx = Path(f"storage/exports/api_test_{job_id}.docx")
    urllib.request.urlretrieve(
        f"{API}/api/ocr/result/{job_id}/export-docx",
        out_docx,
    )
    print(f"   Da tai: {out_docx} ({out_docx.stat().st_size // 1024} KB)")

    print("5. Re-upload Word da sua...")
    re = _post_multipart(
        f"{API}/api/ocr/upload-docx",
        {"job_id": job_id},
        {
            "file": (
                out_docx.name,
                out_docx.read_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    msg = re.get("message", str(re))
    print(f"   OK: {msg.encode('ascii', 'replace').decode()}")

    val = _get(f"{API}/api/ocr/result/{job_id}/validation")
    print(f"6. Validation: {val.get('error_count', 0)} loi")

    if qso < 10:
        print("WARN: it IPCAS QSO* — co the chua doc text PDF")
        return 1

    print("\n=== API TEST PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
