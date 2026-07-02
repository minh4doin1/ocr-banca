"""
OCR Router — API endpoints for PDF upload, OCR processing, review, and export.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from threading import Thread

import aiofiles
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse

from app.config import settings
from app.models.schemas import (
    ErrorResponse,
    JobInfo,
    JobStatus,
    OcrResult,
    ProcessingMode,
    UpdateResultRequest,
    UploadResponse,
)
from app.services.excel_service import export_to_excel
from app.services.table_service import (
    create_job,
    get_all_jobs,
    get_job,
    get_result,
    process_job,
    update_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ocr", tags=["OCR"])

# Allowed file types
ALLOWED_EXTENSIONS = {".pdf"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


@router.post(
    "/upload",
    response_model=UploadResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    summary="Upload PDF cho OCR",
    description="Upload file PDF để trích xuất bảng dữ liệu. "
    "File sẽ được xử lý bất đồng bộ.",
)
async def upload_pdf(
    file: UploadFile = File(..., description="File PDF cần OCR"),
    processing_mode: str = Form(
        default="local",
        description="Chế độ OCR: local, api hoặc auto",
    ),
    api_provider: str = Form(
        default="",
        description="Tên provider khi processing_mode=api (vd: ocrspace)",
    ),
):
    """Upload a PDF file for OCR processing."""

    # Validate file extension
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ file PDF. Nhận được: {ext}",
        )

    # Read and validate file size
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn. Tối đa: {MAX_FILE_SIZE // (1024*1024)} MB",
        )

    # Generate job ID
    job_id = str(uuid.uuid4())[:8]

    # Save file to disk
    pdf_path = settings.upload_path / f"{job_id}_{file.filename}"
    async with aiofiles.open(pdf_path, "wb") as f:
        await f.write(content)

    logger.info("PDF uploaded: %s → %s", file.filename, pdf_path.name)

    # Parse processing mode/provider from multipart form
    processing_mode_str = processing_mode.strip().lower()
    api_provider = api_provider.strip().lower()

    try:
        processing_mode = ProcessingMode(processing_mode_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"processing_mode không hợp lệ: {processing_mode_str}. Chỉ nhận local|api|auto",
        )

    if processing_mode == ProcessingMode.API and not api_provider:
        api_provider = settings.ocr_api_provider

    # Create job record
    create_job(
        job_id,
        file.filename,
        pdf_path,
        processing_mode=processing_mode,
        api_provider=api_provider,
    )

    # Start OCR processing in background thread
    # In production, use Celery instead
    thread = Thread(
        target=_run_ocr_job,
        args=(job_id, pdf_path, processing_mode, api_provider),
        daemon=True,
    )
    thread.start()

    return UploadResponse(
        job_id=job_id,
        filename=file.filename,
        processing_mode=processing_mode,
        api_provider=api_provider,
        message="PDF đã upload thành công. Đang xử lý OCR...",
    )


def _run_ocr_job(
    job_id: str,
    pdf_path: Path,
    processing_mode: ProcessingMode,
    api_provider: str,
) -> None:
    """Run OCR processing in a background thread."""
    try:
        process_job(
            job_id,
            pdf_path,
            processing_mode=processing_mode,
            api_provider=api_provider,
        )
    except Exception as e:
        logger.error("[%s] Background OCR job failed: %s", job_id, e)


@router.get(
    "/status/{job_id}",
    response_model=JobInfo,
    responses={404: {"model": ErrorResponse}},
    summary="Kiểm tra trạng thái OCR",
)
async def get_job_status(job_id: str):
    """Check the status of an OCR job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy job: {job_id}",
        )
    return job


@router.get(
    "/result/{job_id}",
    response_model=OcrResult,
    responses={404: {"model": ErrorResponse}, 202: {"model": JobInfo}},
    summary="Lấy kết quả OCR",
    description="Trả về kết quả OCR bao gồm tất cả bảng đã trích xuất.",
)
async def get_ocr_result(job_id: str):
    """Get the OCR result for a completed job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy job: {job_id}")

    if job.status == JobStatus.PROCESSING:
        raise HTTPException(
            status_code=202,
            detail=f"Job đang xử lý ({job.progress}/{job.total_pages} trang)",
        )

    if job.status == JobStatus.FAILED:
        raise HTTPException(
            status_code=500,
            detail=f"Job thất bại: {job.error_message}",
        )

    if job.status == JobStatus.PENDING:
        raise HTTPException(
            status_code=202,
            detail="Job đang chờ xử lý",
        )

    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Kết quả OCR không tìm thấy",
        )
    return result


@router.put(
    "/result/{job_id}",
    response_model=OcrResult,
    responses={404: {"model": ErrorResponse}},
    summary="Cập nhật kết quả sau review",
    description="Cập nhật giá trị các cell sau khi user kiểm tra.",
)
async def update_ocr_result(job_id: str, request: UpdateResultRequest):
    """Update OCR result cells after user review."""
    try:
        result = update_result(job_id, request.updates)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/result/{job_id}/export",
    summary="Xuất Excel",
    description="Xuất kết quả OCR ra file Excel (.xlsx)",
)
async def export_excel(job_id: str):
    """Export OCR result as an Excel file."""
    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kết quả OCR không tìm thấy cho job: {job_id}",
        )

    try:
        excel_path = export_to_excel(result)
        return FileResponse(
            path=str(excel_path),
            filename=excel_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        logger.error("Excel export failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Lỗi xuất Excel: {e}",
        )


@router.get(
    "/jobs",
    response_model=list[JobInfo],
    summary="Danh sách jobs",
)
async def list_jobs():
    """List all OCR jobs."""
    return get_all_jobs()


@router.get(
    "/result/{job_id}/page/{page_number}/image",
    summary="Ảnh trang PDF",
    description="Lấy ảnh gốc của trang PDF (dùng cho UI review)",
)
async def get_page_image(job_id: str, page_number: int):
    """Get the rendered page image for split-view review UI."""
    image_dir = settings.images_path / job_id
    image_path = image_dir / f"page_{page_number:03d}.png"

    if not image_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Không tìm thấy ảnh trang {page_number}",
        )

    return FileResponse(
        path=str(image_path),
        media_type="image/png",
    )
