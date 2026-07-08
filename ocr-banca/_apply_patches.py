"""Temporary patch script for plan implementation."""
from pathlib import Path

p = Path("ocr-service/app/services/ocr_service.py")
text = p.read_text(encoding="utf-8")
print("ocr len", len(text))
