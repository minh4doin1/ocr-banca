"""
OCR Router — API endpoints for PDF upload, OCR processing, review, and export.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse

from app.config import settings
from app.deps import verify_worker_token
from app.models.schemas import (
    ErrorResponse,
    JobInfo,
    JobStatus,
    OcrEnvProfile,
    OcrEnvironmentsResponse,
    OcrResult,
    OcrRuntimeConfig,
    OcrValidationResponse,
    ProcessingMode,
    RemoteProvider,
    RemoteWorkerHealth,
    UpdateResultRequest,
    UploadResponse,
)
from app.services.docx_service import export_to_docx, import_from_docx
from app.services.excel_service import export_to_excel, import_from_excel
from app.services.job_queue import OcrJobTask, QueueFullError, start_job_queue
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
    update_job_queue_position,
    update_result,
    reocr_page,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/ocr",
    tags=["OCR"],
    dependencies=[Depends(verify_worker_token)],
)

ALLOWED_EXTENSIONS = {".pdf"}
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
ALLOWED_DOCX_EXTENSIONS = {".docx"}
MAX_FILE_SIZE = 50 * 1024 * 1024
_queue_started = False


def init_ocr_worker() -> None:
    """Start bounded OCR job queue (call once at app startup)."""
    global _queue_started
    if not _queue_started:
        start_job_queue(_run_ocr_task)
        _queue_started = True


def _ensure_queue():
    init_ocr_worker()
    from app.services.job_queue import get_job_queue

    return get_job_queue()


def _run_ocr_task(task: OcrJobTask) -> None:
    job = get_job(task.job_id)
    if job:
        job.status = JobStatus.PROCESSING
        update_job_queue_position(task.job_id, 0)
    _run_ocr_job(
        task.job_id,
        task.pdf_path,
        task.processing_mode,
        task.api_provider,
        task.use_gpu,
        task.remote_provider,
        task.remote_url,
        task.remote_token,
    )


@router.get("/queue", summary="Trạng thái hàng đợi OCR")
async def get_queue_status():
    init_ocr_worker()
    from app.services.job_queue import get_job_queue

    return get_job_queue().stats()


@router.get(
    "/environments",
    response_model=OcrEnvironmentsResponse,
    summary="Danh sách môi trường API (DEV/PROD)",
)
async def get_environments():
    """Cho FE biết Keycloak prod đã cấu hình.

    api_url để trống = FE dùng same-origin (tránh ép localhost khi mở qua LAN/Tailscale).
    """
    prod_kc = settings.keycloak_prod_configured
    profiles = [
        OcrEnvProfile(
            id="dev",
            label=settings.ocr_env_dev_label.strip() or "DEV",
            api_url="",
            keycloak_configured=settings.keycloak_configured,
            keycloak_label=settings.keycloak_base_url.strip() or "DEV",
        ),
    ]
    if prod_kc:
        profiles.append(
            OcrEnvProfile(
                id="prod",
                label=settings.ocr_env_prod_label.strip() or "PROD",
                api_url="",
                keycloak_configured=True,
                keycloak_label=settings.keycloak_prod_base_url.strip() or "PROD",
            )
        )
    return OcrEnvironmentsResponse(
        server_env=(settings.app_env.strip().lower() or "dev"),
        profiles=profiles,
    )


@router.get(
    "/config",
    response_model=OcrRuntimeConfig,
    summary="Cấu hình runtime OCR",
)
async def get_runtime_config():
    """Expose non-secret runtime options for the frontend."""
    from app.services.gpu_runtime import probe_gpu_runtime

    gpu = probe_gpu_runtime()
    internal_url = settings.resolve_internal_gpu_url(local_gpu_ok=gpu.paddle_gpu_ok)
    label = ""
    if internal_url:
        if internal_url.startswith(("http://127.0.0.1", "http://localhost")):
            label = gpu.gpu_name or "GPU máy chủ"
        else:
            label = internal_url.replace("http://", "").replace("https://", "")[:48]

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

    task = OcrJobTask(
        job_id=job_id,
        pdf_path=pdf_path,
        processing_mode=mode,
        api_provider=api_provider,
        use_gpu=use_gpu_flag,
        remote_provider=remote_provider_enum,
        remote_url=remote_url,
        remote_token=remote_token,
    )
    try:
        queue_position = _ensure_queue().submit(task)
    except QueueFullError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    msg = "PDF đã upload thành công. Đang xử lý OCR..."
    if queue_position > 1:
        msg = (
            f"PDF đã upload. Đang xếp hàng (vị trí {queue_position}) — "
            "GPU xử lý tuần tự, vui lòng chờ."
        )

    return UploadResponse(
        job_id=job_id,
        filename=file.filename,
        processing_mode=mode,
        api_provider=api_provider,
        use_gpu=use_gpu_flag,
        remote_provider=remote_provider_enum,
        remote_url=remote_url,
        queue_position=queue_position,
        message=msg,
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


@router.post(
    "/upload-docx",
    response_model=UploadResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    summary="Upload Word (.docx) để nạp dữ liệu trực tiếp (bỏ qua OCR)",
)
async def upload_docx(
    file: UploadFile = File(..., description="File Word .docx chứa bảng danh sách"),
    job_id: str = Form(default=""),
):
    """Đọc bảng trong file Word gốc và map vào cấu trúc OCR (không qua OCR)."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_DOCX_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ file Word (.docx). Nhận được: {ext}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn. Tối đa: {MAX_FILE_SIZE // (1024 * 1024)} MB",
        )

    target_job_id = job_id.strip() or str(uuid.uuid4())[:8]
    docx_path = settings.upload_path / f"{target_job_id}_{file.filename}"
    async with aiofiles.open(docx_path, "wb") as f:
        await f.write(content)

    try:
        result = import_from_docx(docx_path, target_job_id, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        message="Đã nạp dữ liệu từ Word. Có thể đối chiếu/chỉnh sửa ngay.",
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
    from app.services.remote_ocr_service import should_run_local_gpu_job

    try:
        if (
            processing_mode == ProcessingMode.REMOTE
            and remote_provider
            and should_run_local_gpu_job(remote_provider, remote_url)
        ):
            process_job(
                job_id,
                pdf_path,
                processing_mode=ProcessingMode.LOCAL,
                api_provider=api_provider,
                use_gpu=True,
            )
        elif processing_mode == ProcessingMode.REMOTE and remote_provider:
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


@router.post(
    "/result/{job_id}/page/{page_number}/reocr",
    response_model=OcrResult,
    summary="OCR lại một trang PDF",
)
async def reocr_single_page(job_id: str, page_number: int):
    try:
        reocr_page(job_id, page_number)
        result = get_result(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Kết quả không tìm thấy")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Re-OCR page failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/result/{job_id}/validation", response_model=OcrValidationResponse)
async def get_ocr_validation(job_id: str):
    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kết quả OCR không tìm thấy cho job: {job_id}",
        )
    from app.services.user_mapping import validate_ocr_result

    data = validate_ocr_result(result)
    return OcrValidationResponse(**data)


@router.get("/result/{job_id}/export")
async def export_excel(job_id: str, pages: str | None = None):
    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kết quả OCR không tìm thấy cho job: {job_id}",
        )

    page_numbers: list[int] | None = None
    if pages:
        try:
            page_numbers = [int(p.strip()) for p in pages.split(",") if p.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Tham số pages không hợp lệ (vd: pages=1,2,3)",
            ) from exc
        if not page_numbers:
            raise HTTPException(status_code=400, detail="Chưa chọn trang để xuất")

    try:
        excel_path = export_to_excel(result, page_numbers=page_numbers)
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


@router.get("/result/{job_id}/export-docx")
async def export_docx(job_id: str, pages: str | None = None):
    """Xuất kết quả ra file Word (.docx)."""
    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Kết quả OCR không tìm thấy cho job: {job_id}",
        )

    page_numbers: list[int] | None = None
    if pages:
        try:
            page_numbers = [int(p.strip()) for p in pages.split(",") if p.strip()]
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="Tham số pages không hợp lệ (vd: pages=1,2,3)",
            ) from exc
        if not page_numbers:
            raise HTTPException(status_code=400, detail="Chưa chọn trang để xuất")

    try:
        docx_path = export_to_docx(result, page_numbers=page_numbers)
        return FileResponse(
            path=str(docx_path),
            filename=docx_path.name,
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.error("DOCX export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Lỗi xuất Word: {e}")


@router.get("/result/{job_id}/pdf-to-docx")
async def convert_pdf_to_docx_download(job_id: str):
    """Chuyển file PDF gốc của job sang Word (.docx) để sửa tay rồi nạp lại."""
    from app.services.pdf_text_service import convert_pdf_to_docx

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy job: {job_id}")

    candidates = list(settings.upload_path.glob(f"{job_id}_*.pdf"))
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy file PDF gốc của job",
        )
    pdf_path = candidates[0]
    out_path = settings.export_path / f"{job_id}_{pdf_path.stem}.docx"
    try:
        convert_pdf_to_docx(pdf_path, out_path)
        return FileResponse(
            path=str(out_path),
            filename=out_path.name,
            media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.error("PDF to DOCX failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Lỗi chuyển PDF sang Word: {e}")


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
