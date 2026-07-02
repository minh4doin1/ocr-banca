"""
Table Service — Orchestrates the full OCR pipeline for a PDF.

Manages the end-to-end flow:
  PDF → pages → OCR per page → aggregate results → store
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models.schemas import (
    JobInfo,
    JobStatus,
    OcrResult,
    PageResult,
    ProcessingMode,
    UpdateCellRequest,
)
from app.services.ocr_api_service import process_page_via_api
from app.services.ocr_service import process_page
from app.services.pdf_service import convert_pdf_to_images, get_page_count

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# In-memory job store (replace with Redis/DB in production)
# ──────────────────────────────────────────────────────────────

_jobs: dict[str, JobInfo] = {}
_results: dict[str, OcrResult] = {}


def get_job(job_id: str) -> JobInfo | None:
    """Get job info by ID."""
    return _jobs.get(job_id)


def get_all_jobs() -> list[JobInfo]:
    """Get all jobs, newest first."""
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


def get_result(job_id: str) -> OcrResult | None:
    """Get OCR result by job ID. Try memory first, then disk."""
    if job_id in _results:
        return _results[job_id]

    # Try loading from disk
    result_file = settings.result_path / f"{job_id}.json"
    if result_file.exists():
        try:
            data = json.loads(result_file.read_text(encoding="utf-8"))
            result = OcrResult(**data)
            _results[job_id] = result
            return result
        except Exception as e:
            logger.error("Failed to load result from disk: %s", e)

    return None


def create_job(
    job_id: str,
    filename: str,
    pdf_path: str | Path,
    processing_mode: ProcessingMode = ProcessingMode.LOCAL,
    api_provider: str = "",
) -> JobInfo:
    """Create a new OCR job."""
    pdf_path = Path(pdf_path)
    total_pages = get_page_count(pdf_path)

    job = JobInfo(
        job_id=job_id,
        filename=filename,
        processing_mode=processing_mode,
        api_provider=api_provider,
        status=JobStatus.PENDING,
        total_pages=total_pages,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    _jobs[job_id] = job
    return job


def process_job(
    job_id: str,
    pdf_path: str | Path,
    processing_mode: ProcessingMode = ProcessingMode.LOCAL,
    api_provider: str = "",
) -> OcrResult:
    """
    Process a PDF through the full OCR pipeline.

    This is the main entry point called after upload.
    In production, this would be dispatched to a Celery worker.

    Args:
        job_id: Unique job identifier
        pdf_path: Path to the uploaded PDF

    Returns:
        OcrResult with all extracted tables
    """
    job = _jobs.get(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    try:
        # Update status
        job.status = JobStatus.PROCESSING
        job.updated_at = datetime.now()

        # Step 1: Convert PDF to images
        logger.info("[%s] Converting PDF to images …", job_id)
        image_paths = convert_pdf_to_images(pdf_path, job_id)
        job.total_pages = len(image_paths)

        # Step 2: OCR each page
        pages: list[PageResult] = []
        provider = api_provider or settings.ocr_api_provider
        for i, image_path in enumerate(image_paths, start=1):
            logger.info(
                "[%s] Processing page %d/%d …", job_id, i, len(image_paths)
            )
            if processing_mode == ProcessingMode.API:
                page_result = process_page_via_api(
                    image_path=image_path,
                    page_number=i,
                    provider=provider,
                )
            elif processing_mode == ProcessingMode.AUTO:
                try:
                    page_result = process_page(image_path, page_number=i)
                except Exception as local_error:
                    logger.warning(
                        "[%s] Local OCR failed on page %d, fallback API (%s): %s",
                        job_id,
                        i,
                        provider,
                        local_error,
                    )
                    page_result = process_page_via_api(
                        image_path=image_path,
                        page_number=i,
                        provider=provider,
                    )
            else:
                page_result = process_page(image_path, page_number=i)
            pages.append(page_result)

            # Update progress
            job.progress = i
            job.updated_at = datetime.now()

        # Step 3: Aggregate result
        result = OcrResult(
            job_id=job_id,
            filename=job.filename,
            total_pages=len(pages),
            pages=pages,
            created_at=job.created_at,
            updated_at=datetime.now(),
        )

        # Save result to memory and disk
        _results[job_id] = result
        _save_result_to_disk(result)

        # Mark job as completed
        job.status = JobStatus.COMPLETED
        job.updated_at = datetime.now()

        logger.info("[%s] Job completed successfully", job_id)
        return result

    except Exception as e:
        logger.error("[%s] Job failed: %s", job_id, e)
        job.status = JobStatus.FAILED
        job.error_message = str(e)
        job.updated_at = datetime.now()
        raise


def update_result(job_id: str, updates: list[UpdateCellRequest]) -> OcrResult:
    """
    Update OCR result cells after user review.

    Args:
        job_id: Job identifier
        updates: List of cell updates

    Returns:
        Updated OcrResult
    """
    result = get_result(job_id)
    if result is None:
        raise ValueError(f"Result not found for job: {job_id}")

    for update in updates:
        # Find the matching cell and update
        for page in result.pages:
            if page.page_number != update.page_number:
                continue
            for table in page.tables:
                if table.table_index != update.table_index:
                    continue
                for cell in table.cells:
                    if cell.row == update.row and cell.col == update.col:
                        cell.text = update.text
                        cell.confidence = 1.0  # User-verified
                        break

    result.updated_at = datetime.now()

    # Persist
    _results[job_id] = result
    _save_result_to_disk(result)

    logger.info("[%s] Updated %d cell(s)", job_id, len(updates))
    return result


def _save_result_to_disk(result: OcrResult) -> None:
    """Persist OCR result to disk as JSON."""
    result_file = settings.result_path / f"{result.job_id}.json"
    try:
        result_file.write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.debug("Result saved to %s", result_file)
    except Exception as e:
        logger.error("Failed to save result to disk: %s", e)
