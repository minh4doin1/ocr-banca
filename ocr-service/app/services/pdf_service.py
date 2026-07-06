"""
PDF Service — Convert PDF pages to images.

Uses pdf2image (backed by Poppler) to render each page of a PDF
at high DPI for OCR processing.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path

from PIL import Image
from pdf2image import convert_from_path

from app.config import settings

logger = logging.getLogger(__name__)

# Poppler (pdftoppm) treo trên Windows khi chạy song song với Paddle GPU.
_PDF_CONVERT_LOCK = threading.Lock()
_convert_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-convert")


def _configured_poppler_path() -> str:
    """POPPLER_PATH from env — empty/whitespace means use system PATH."""
    return (settings.poppler_path or "").strip()


def _get_poppler_path() -> str | None:
    """Get poppler path from configuration or auto-detect."""
    configured = _configured_poppler_path()
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path.resolve())
        logger.warning("POPPLER_PATH configured but not found: %s", path)

    # Linux/macOS Colab/Docker: dùng poppler-utils trong PATH
    if platform.system() != "Windows":
        if shutil.which("pdftoppm"):
            logger.info("Using system Poppler from PATH")
            return None
        for candidate in ("/usr/bin", "/usr/local/bin"):
            if (Path(candidate) / "pdftoppm").exists():
                logger.info("Using Poppler at %s", candidate)
                return candidate
        logger.error(
            "Poppler not found. Install: apt-get install poppler-utils (Linux)"
        )
        return None

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


def _poppler_convert_kwargs() -> dict:
    """pdf2image kwargs — omit poppler_path when using system PATH."""
    poppler_path = _get_poppler_path()
    if poppler_path:
        return {"poppler_path": poppler_path}
    return {}


def _kill_stuck_poppler_processes() -> None:
    """Best-effort cleanup when pdftoppm hangs (Windows + concurrent GPU OCR)."""
    if platform.system() == "Windows":
        for name in ("pdftoppm.exe", "pdfinfo.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", name],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except Exception:
                pass
    else:
        for name in ("pdftoppm", "pdfinfo"):
            found = shutil.which(name)
            if found:
                try:
                    subprocess.run(
                        ["pkill", "-f", name],
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                except Exception:
                    pass


def _run_poppler_with_timeout(fn, *, action: str):
    """Serialize Poppler calls and abort if pdftoppm hangs."""
    timeout = max(30, settings.pdf_convert_timeout_seconds)

    def _wrapped():
        with _PDF_CONVERT_LOCK:
            return fn()

    future = _convert_pool.submit(_wrapped)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError as exc:
        _kill_stuck_poppler_processes()
        raise RuntimeError(
            f"Poppler timeout sau {timeout}s khi {action}. "
            "Thử giảm PDF_DPI hoặc tắt prefetch."
        ) from exc


def check_poppler_available() -> tuple[bool, str]:
    """Verify Poppler binaries are reachable (for health checks)."""
    poppler_path = _get_poppler_path()
    if poppler_path:
        pdfinfo = Path(poppler_path) / (
            "pdfinfo.exe" if platform.system() == "Windows" else "pdfinfo"
        )
        if pdfinfo.exists():
            return True, str(poppler_path)
        return False, f"POPPLER_PATH không có pdfinfo: {poppler_path}"

    for tool in ("pdfinfo", "pdftoppm"):
        found = shutil.which(tool)
        if found:
            return True, f"PATH:{found}"
    return False, "poppler-utils chưa cài (apt-get install poppler-utils)"


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

    ok, poppler_info = check_poppler_available()
    if not ok:
        raise RuntimeError(
            f"{poppler_info}. Linux/Colab: `apt-get install -y poppler-utils`"
        )
    logger.info("Using Poppler: %s", poppler_info)

    try:
        # Convert all pages (serialized — tránh treo pdftoppm trên Windows)
        pages: list[Image.Image] = _run_poppler_with_timeout(
            lambda: convert_from_path(
                str(pdf_path),
                dpi=dpi,
                fmt="png",
                thread_count=1,
                **_poppler_convert_kwargs(),
            ),
            action=f"convert toàn bộ {pdf_path.name}",
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


def convert_pdf_page(
    pdf_path: str | Path,
    job_id: str,
    page_number: int,
    dpi: int | None = None,
) -> Path:
    """
    Convert a single PDF page to PNG (lazy pipeline — OCR trang 1 nhanh hơn).
    """
    pdf_path = Path(pdf_path)
    if page_number < 1:
        raise ValueError("page_number must be >= 1")

    if dpi is None:
        dpi = settings.pdf_dpi

    output_dir = settings.images_path / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"page_{page_number:03d}.png"
    if image_path.exists() and image_path.stat().st_size > 0:
        return image_path

    ok, poppler_info = check_poppler_available()
    if not ok:
        raise RuntimeError(f"{poppler_info}")

    pages = _run_poppler_with_timeout(
        lambda: convert_from_path(
            str(pdf_path),
            dpi=dpi,
            fmt="png",
            first_page=page_number,
            last_page=page_number,
            thread_count=1,
            **_poppler_convert_kwargs(),
        ),
        action=f"convert trang {page_number} của {pdf_path.name}",
    )
    if not pages:
        raise RuntimeError(f"Không convert được trang {page_number} của PDF")

    pages[0].save(str(image_path), "PNG", optimize=True)
    logger.debug("Saved page %d → %s (Poppler: %s)", page_number, image_path.name, poppler_info)
    return image_path


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

    try:
        info = pdfinfo_from_path(str(pdf_path), **_poppler_convert_kwargs())
        return info.get("Pages", 0)
    except Exception as e:
        logger.warning("Could not get page count: %s", e)
        return 0

