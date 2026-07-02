"""
PDF Service — Convert PDF pages to images.

Uses pdf2image (backed by Poppler) to render each page of a PDF
at high DPI for OCR processing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image
from pdf2image import convert_from_path

from app.config import settings

logger = logging.getLogger(__name__)


def _get_poppler_path() -> str | None:
    """Get poppler path from configuration or auto-detect in project directory."""
    if settings.poppler_path:
        path = Path(settings.poppler_path)
        if path.exists():
            return str(path.resolve())
            
    project_root = Path(__file__).resolve().parents[2]
    bin_dir = project_root / "bin"
    
    # 1. Check direct standard paths first
    local_paths = [
        bin_dir / "poppler" / "Library" / "bin",
        bin_dir / "poppler" / "bin",
        bin_dir / "poppler",
    ]
    for lp in local_paths:
        if (lp / "pdftoppm.exe").exists() or (lp / "pdftoppm").exists():
            logger.info("Auto-detected local Poppler path: %s", lp)
            return str(lp.resolve())

    # 2. Dynamic scan of any extracted folder under bin/ (e.g. poppler-24.08.0)
    if bin_dir.exists():
        try:
            for item in bin_dir.iterdir():
                if item.is_dir() and (item.name.startswith("poppler") or item.name.startswith("Release")):
                    check_paths = [
                        item / "Library" / "bin",
                        item / "bin",
                        item
                    ]
                    for cp in check_paths:
                        if (cp / "pdftoppm.exe").exists() or (cp / "pdftoppm").exists():
                            logger.info("Auto-detected dynamically scanned Poppler path: %s", cp)
                            return str(cp.resolve())
        except Exception as e:
            logger.debug("Failed to dynamically scan bin directory: %s", e)
            
    return None


def convert_pdf_to_images(
    pdf_path: str | Path,
    job_id: str,
    dpi: int | None = None,
) -> list[Path]:
    """
    Convert a PDF file to a list of page images.

    Each page is saved as a PNG file in the images directory,
    named as {job_id}_page_{page_number}.png.

    Args:
        pdf_path: Path to the PDF file
        dpi: Resolution for rendering. Higher = better OCR but slower.
             Defaults to settings.pdf_dpi (300).
        job_id: Job ID for naming output files

    Returns:
        List of paths to the generated page images

    Raises:
        FileNotFoundError: If PDF file doesn't exist
        RuntimeError: If pdf2image/Poppler fails
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    if dpi is None:
        dpi = settings.pdf_dpi

    # Create output directory for this job
    output_dir = settings.images_path / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Converting PDF to images: %s (DPI: %d)", pdf_path.name, dpi
    )

    poppler_path = _get_poppler_path()
    if poppler_path:
        logger.info("Using Poppler path: %s", poppler_path)

    try:
        # Convert all pages
        pages: list[Image.Image] = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            fmt="png",
            thread_count=2,
            poppler_path=poppler_path,
        )

        image_paths: list[Path] = []
        for i, page in enumerate(pages, start=1):
            image_path = output_dir / f"page_{i:03d}.png"
            page.save(str(image_path), "PNG")
            image_paths.append(image_path)
            logger.debug("Saved page %d → %s", i, image_path.name)

        logger.info(
            "PDF conversion complete: %d pages generated", len(image_paths)
        )
        return image_paths

    except Exception as e:
        logger.error("Failed to convert PDF: %s", e)
        raise RuntimeError(
            f"Failed to convert PDF to images: {e}. "
            "Make sure Poppler is installed (poppler-utils)."
        ) from e


def get_page_count(pdf_path: str | Path) -> int:
    """
    Get the number of pages in a PDF without full conversion.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        Number of pages
    """
    from pdf2image import pdfinfo_from_path

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    poppler_path = _get_poppler_path()

    try:
        info = pdfinfo_from_path(str(pdf_path), poppler_path=poppler_path)
        return info.get("Pages", 0)
    except Exception as e:
        logger.warning("Could not get page count: %s", e)
        return 0

