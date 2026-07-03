"""
OCR Router — API endpoints for PDF upload, OCR processing, review, and export.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from threading import Thread

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse

from app.config import settings
from app.deps import verify_worker_token
from app.models.schemas import (
    ErrorResponse,
    JobInfo,
    JobStatus,
    OcrResult,
    OcrRuntimeConfig,
    ProcessingMode,
    RemoteProvider,
    RemoteWorkerHealth,
    UpdateResultRequest,
    UploadResponse,
)
from app.services.excel_service import export_to_excel, import_from_excel
from app.services.remote_ocr_service import (
    check_worker_health,
    resolve_remote_target,
)
from app.services.table_service import (
    create_manual_job,
    create_job,
    get_all_jobs,
    get_job,
    get_job_remote_token,
    get_result,
    process_job,
    process_remote_job,
    set_job_remote_token,
    set_result,
    update_result,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/ocr",
    tags=["OCR"],
    dependencies=[Depends(verify_worker_token)],
)

ALLOWED_EXTENSIONS = {".pdf"}
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
MAX_FILE_SIZE = 50 * 1024 * 1024


@router.get(
    "/config",
    response_model=OcrRuntimeConfig,
    summary="Cấu hình runtime OCR",
)
async def get_runtime_config():
    """Expose non-secret runtime options for the frontend."""
    from app.services.gpu_runtime import probe_gpu_runtime

    internal_url = settings.internal_gpu_url.strip()
    label = ""
    if internal_url:
        label = internal_url.replace("http://", "").replace("https://", "")[:48]

    gpu = probe_gpu_runtime()
    return OcrRuntimeConfig(
        internal_gpu_configured=bool(internal_url),
        internal_gpu_label=label,
        worker_token_required=bool(settings.remote_worker_token.strip()),
        local_gpu_available=gpu.paddle_gpu_ok,
        local_gpu_name=gpu.gpu_name,
        local_gpu_detail=gpu.detail,
        paddle_use_gpu=settings.paddle_use_gpu,
    )


@router.get(
    "/worker/health",
    response_model=RemoteWorkerHealth,
    summary="Kiểm tra worker GPU remote",
)
async def get_worker_health(
    provider: str = Query(default="colab"),
    url: str = Query(default=""),
    token: str = Query(default=""),
):
    """Test connectivity to an internal GPU server or Colab tunnel."""
    try:
        remote_provider = RemoteProvider(provider.strip().lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="provider không hợp lệ: internal|colab|custom",
        )

    try:
        worker_url, worker_token = resolve_remote_target(
            remote_provider, url, token
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return check_worker_health(worker_url, worker_token)


@router.post(
    "/upload",
    response_model=UploadResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    summary="Upload PDF cho OCR",
)
async def upload_pdf(
    file: UploadFile = File(..., description="File PDF cần OCR"),
    processing_mode: str = Form(default="local"),
    api_provider: str = Form(default=""),
    use_gpu: str = Form(default="false"),
    remote_provider: str = Form(default=""),
    remote_url: str = Form(default=""),
    remote_token: str = Form(default=""),
):
    """Upload a PDF file for OCR processing."""

    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ file PDF. Nhận được: {ext}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn. Tối đa: {MAX_FILE_SIZE // (1024 * 1024)} MB",
        )

    job_id = str(uuid.uuid4())[:8]
    pdf_path = settings.upload_path / f"{job_id}_{file.filename}"
    async with aiofiles.open(pdf_path, "wb") as f:
        await f.write(content)

    logger.info("PDF uploaded: %s → %s", file.filename, pdf_path.name)

    processing_mode_str = processing_mode.strip().lower()
    api_provider = api_provider.strip().lower()
    remote_provider_str = remote_provider.strip().lower()
    remote_url = remote_url.strip()
    remote_token = remote_token.strip()

    try:
        mode = ProcessingMode(processing_mode_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"processing_mode không hợp lệ: {processing_mode_str}. "
                "Chỉ nhận local|remote|api|auto"
            ),
        )

    remote_provider_enum: RemoteProvider | None = None
    if mode == ProcessingMode.REMOTE:
        if not remote_provider_str:
            remote_provider_str = RemoteProvider.INTERNAL.value
        try:
            remote_provider_enum = RemoteProvider(remote_provider_str)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="remote_provider không hợp lệ: internal|colab|custom",
            )
        try:
            resolved_url, _ = resolve_remote_target(remote_provider_enum, remote_url)
            remote_url = resolved_url
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if mode == ProcessingMode.API and not api_provider:
        api_provider = settings.ocr_api_provider

    use_gpu_flag = use_gpu.strip().lower() in ("true", "1", "yes", "on")
    if mode == ProcessingMode.REMOTE:
        use_gpu_flag = True

    create_job(
        job_id,
        file.filename,
        pdf_path,
        processing_mode=mode,
        api_provider=api_provider,
        use_gpu=use_gpu_flag,
        remote_provider=remote_provider_enum,
        remote_url=remote_url,
    )
    if mode == ProcessingMode.REMOTE:
        set_job_remote_token(job_id, remote_token)

    thread = Thread(
        target=_run_ocr_job,
        args=(
            job_id,
            pdf_path,
            mode,
            api_provider,
            use_gpu_flag,
            remote_provider_enum,
            remote_url,
            remote_token,
        ),
        daemon=True,
    )
    thread.start()

    return UploadResponse(
        job_id=job_id,
        filename=file.filename,
        processing_mode=mode,
        api_provider=api_provider,
        use_gpu=use_gpu_flag,
        remote_provider=remote_provider_enum,
        remote_url=remote_url,
        message="PDF đã upload thành công. Đang xử lý OCR...",
    )


@router.post(
    "/upload-excel",
    response_model=UploadResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    summary="Upload Excel để nạp dữ liệu trực tiếp (bỏ qua OCR)",
)
async def upload_excel(
    file: UploadFile = File(..., description="File Excel đã chỉnh"),
    job_id: str = Form(default=""),
):
    """Upload Excel and map its table data into OCR result structure."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXCEL_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ file Excel (.xlsx/.xlsm). Nhận được: {ext}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn. Tối đa: {MAX_FILE_SIZE // (1024 * 1024)} MB",
        )

    target_job_id = job_id.strip() or str(uuid.uuid4())[:8]
    excel_path = settings.upload_path / f"{target_job_id}_{file.filename}"
    async with aiofiles.open(excel_path, "wb") as f:
        await f.write(content)

    result = import_from_excel(excel_path, target_job_id, file.filename)

    job = get_job(target_job_id)
    if job is None:
        create_manual_job(
            job_id=target_job_id,
            filename=file.filename,
            total_pages=result.total_pages,
        )

    set_result(target_job_id, result)

    return UploadResponse(
        job_id=target_job_id,
        filename=file.filename,
        processing_mode=ProcessingMode.LOCAL,
        use_gpu=False,
        message="Đã nạp dữ liệu từ Excel. Có thể đối chiếu/chỉnh sửa ngay.",
    )


def _run_ocr_job(
    job_id: str,
    pdf_path: Path,
    processing_mode: ProcessingMode,
    api_provider: str,
    use_gpu: bool = False,
    remote_provider: RemoteProvider | None = None,
    remote_url: str = "",
    remote_token: str = "",
) -> None:
    """Run OCR processing in a background thread."""
    try:
        if processing_mode == ProcessingMode.REMOTE and remote_provider:
            process_remote_job(
                job_id,
                pdf_path,
                remote_provider=remote_provider,
                remote_url=remote_url,
                use_gpu=use_gpu,
                remote_token=remote_token,
            )
        else:
            process_job(
                job_id,
                pdf_path,
                processing_mode=processing_mode,
                api_provider=api_provider,
                use_gpu=use_gpu,
            )
    except Exception as e:
        logger.error("[%s] Background OCR job failed: %s", job_id, e)


@router.get("/status/{job_id}", response_model=JobInfo)
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy job: {job_id}")
    return job


@router.get("/result/{job_id}", response_model=OcrResult)
async def get_ocr_result(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy job: {job_id}")

    if job.status == JobStatus.FAILED:
        raise HTTPException(
            status_code=500,
            detail=f"Job thất bại: {job.error_message}",
        )

    result = get_result(job_id)

    if result is None:
        if job.status in (JobStatus.PENDING, JobStatus.PROCESSING):
            raise HTTPException(
                status_code=202,
                detail=f"Job đang xử lý ({job.progress}/{job.total_pages} trang)",
            )
        raise HTTPException(status_code=404, detail="Kết quả OCR không tìm thấy")

    result.is_complete = job.status == JobStatus.COMPLETED
    return result


@router.put("/result/{job_id}", response_model=OcrResult)
async def update_ocr_result(job_id: str, request: UpdateResultRequest):
    try:
        return update_result(job_id, request.updates)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/result/{job_id}/export")
async def export_excel(job_id: str):
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
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
    except Exception as e:
        logger.error("Excel export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Lỗi xuất Excel: {e}")


@router.get("/jobs", response_model=list[JobInfo])
async def list_jobs():
    return get_all_jobs()


@router.get("/result/{job_id}/page/{page_number}/image")
async def get_page_image(job_id: str, page_number: int):
    image_dir = settings.images_path / job_id
    image_path = image_dir / f"page_{page_number:03d}.png"

    if not image_path.exists():
        job = get_job(job_id)
        if job and job.remote_job_id and job.remote_url:
            from app.services.remote_ocr_service import (
                cache_remote_page_image,
                resolve_remote_target,
            )

            try:
                token = get_job_remote_token(job_id)
                if not token and job.remote_provider == RemoteProvider.INTERNAL:
                    _, token = resolve_remote_target(
                        RemoteProvider.INTERNAL, job.remote_url
                    )
                image_path = cache_remote_page_image(
                    job_id,
                    job.remote_url,
                    job.remote_job_id,
                    page_number,
                    token,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Không tìm thấy ảnh trang {page_number}: {exc}",
                )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Không tìm thấy ảnh trang {page_number}",
            )

    return FileResponse(path=str(image_path), media_type="image/png")
