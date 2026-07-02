"""
OCR API service.

Currently supports OCR.space as a lightweight cloud OCR provider.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from app.config import settings
from app.models.schemas import PageResult, TableData

logger = logging.getLogger(__name__)

_OCRSPACE_ENDPOINT = "https://api.ocr.space/parse/image"


def process_page_via_api(
    image_path: str | Path,
    page_number: int,
    provider: str = "ocrspace",
) -> PageResult:
    """Run OCR using remote API for a single page image."""
    provider_key = provider.strip().lower()
    image_path = Path(image_path)

    if provider_key != "ocrspace":
        raise ValueError(f"Unsupported OCR API provider: {provider}")

    raw_text = _ocrspace_extract_text(image_path)
    return PageResult(
        page_number=page_number,
        image_path=str(image_path),
        tables=[],
        raw_text=raw_text,
    )


def _ocrspace_extract_text(image_path: Path) -> str:
    """Extract plain text using OCR.space API."""
    with image_path.open("rb") as f:
        response = requests.post(
            _OCRSPACE_ENDPOINT,
            headers={"apikey": settings.ocrspace_api_key},
            data={
                "language": "vie",
                "isOverlayRequired": "false",
                "OCREngine": "2",
                "scale": "true",
            },
            files={"file": (image_path.name, f, "image/png")},
            timeout=settings.ocr_api_timeout_seconds,
        )

    response.raise_for_status()
    payload = response.json()

    if payload.get("IsErroredOnProcessing"):
        error_message = "; ".join(payload.get("ErrorMessage", []) or ["Unknown OCR API error"])
        raise RuntimeError(f"OCR API failed: {error_message}")

    parsed_results = payload.get("ParsedResults") or []
    if not parsed_results:
        return ""

    page_texts: list[str] = []
    for item in parsed_results:
        text = (item or {}).get("ParsedText", "")
        if text:
            page_texts.append(text.strip())

    result_text = "\n".join(page_texts).strip()
    logger.info("OCR API extracted %d chars from %s", len(result_text), image_path.name)
    return result_text
