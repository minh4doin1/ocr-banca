"""
Table Service — Orchestrates the full OCR pipeline for a PDF.

Manages the end-to-end flow:
  PDF → pages → OCR per page (incremental) → aggregate results → store
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models.schemas import (
    JobInfo,
    JobLogEntry,
    JobStatus,
    LogLevel,
    OcrResult,
    PageResult,
    PageStatus,
    PageStatusInfo,
    ProcessingMode,
    RemoteProvider,
    UpdateCellRequest,
)
from app.services.ocr_api_service import process_page_via_api
from app.services.ocr_service import (
    configure_ocr_device,
    force_cpu_mode,
    is_gpu_runtime_error,
    probe_local_gpu,
    process_page,
)
from app.services.pdf_service import convert_pdf_to_images, get_page_count
from app.services.remote_ocr_service import (
    cache_remote_page_image,
    fetch_remote_result,
    fetch_remote_status,
    is_transient_remote_error,
    mirror_remote_status,
    provider_label,
    resolve_remote_target,
    upload_pdf_to_remote,
)

logger = logging.getLogger(__name__)

MAX_JOB_LOGS = 300

# ──────────────────────────────────────────────────────────────
# In-memory job store (replace with Redis/DB in production)
# ──────────────────────────────────────────────────────────────

_jobs: dict[str, JobInfo] = {}
_results: dict[str, OcrResult] = {}
_job_remote_tokens: dict[str, str] = {}


def set_job_remote_token(job_id: str, token: str) -> None:
    """Store bearer token for remote worker calls (not exposed via API)."""
    if token:
        _job_remote_tokens[job_id] = token.strip()


def get_job_remote_token(job_id: str) -> str:
    return _job_remote_tokens.get(job_id, "")


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

    result_file = settings.result_path / f"{job_id}.json"
    if result_file.exists():
        try:
            raw = result_file.read_text(encoding="utf-8").strip()
            if not raw:
                logger.warning("Result file empty: %s", result_file)
                return None
            data = json.loads(raw)
            result = OcrResult(**data)
            _results[job_id] = result
            return result
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to load result from disk: %s", e)

    return None


def _append_log(
    job: JobInfo,
    message: str,
    level: LogLevel = LogLevel.INFO,
) -> None:
    """Append a log entry visible to the client."""
    job.logs.append(JobLogEntry(message=message, level=level))
    if len(job.logs) > MAX_JOB_LOGS:
        job.logs = job.logs[-MAX_JOB_LOGS:]
    job.updated_at = datetime.now()
    logger.info("[%s] %s", job.job_id, message)


def _set_page_status(
    job: JobInfo,
    page_number: int,
    status: PageStatus,
    error_message: str = "",
) -> None:
    """Update status for a single page."""
    for ps in job.page_statuses:
        if ps.page_number == page_number:
            ps.status = status
            ps.error_message = error_message
            job.updated_at = datetime.now()
            return
    job.page_statuses.append(
        PageStatusInfo(
            page_number=page_number,
            status=status,
            error_message=error_message,
        )
    )
    job.updated_at = datetime.now()


def _init_page_statuses(job: JobInfo, total: int) -> None:
    """Initialize page status list for a job."""
    job.page_statuses = [
        PageStatusInfo(page_number=i, status=PageStatus.PENDING)
        for i in range(1, total + 1)
    ]


def create_job(
    job_id: str,
    filename: str,
    pdf_path: str | Path,
    processing_mode: ProcessingMode = ProcessingMode.LOCAL,
    api_provider: str = "",
    use_gpu: bool = False,
    remote_provider: RemoteProvider | None = None,
    remote_url: str = "",
) -> JobInfo:
    """Create a new OCR job."""
    pdf_path = Path(pdf_path)
    total_pages = get_page_count(pdf_path)

    job = JobInfo(
        job_id=job_id,
        filename=filename,
        processing_mode=processing_mode,
        api_provider=api_provider,
        use_gpu=use_gpu,
        remote_provider=remote_provider,
        remote_url=remote_url,
        status=JobStatus.PENDING,
        total_pages=total_pages,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    _init_page_statuses(job, total_pages)
    _jobs[job_id] = job
    return job


def create_manual_job(
    job_id: str,
    filename: str,
    total_pages: int = 1,
) -> JobInfo:
    """Create an already-completed job used by non-OCR imports (e.g. Excel)."""
    job = JobInfo(
        job_id=job_id,
        filename=filename,
        processing_mode=ProcessingMode.LOCAL,
        use_gpu=False,
        status=JobStatus.COMPLETED,
        total_pages=max(1, total_pages),
        progress=max(1, total_pages),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    _init_page_statuses(job, job.total_pages)
    for ps in job.page_statuses:
        ps.status = PageStatus.COMPLETED
    _jobs[job_id] = job
    return job


def set_result(job_id: str, result: OcrResult) -> OcrResult:
    """Persist imported/manual result and sync job status."""
    _results[job_id] = result
    _save_result_to_disk(result)

    job = _jobs.get(job_id)
    if job:
        job.filename = result.filename
        job.total_pages = result.total_pages
        job.progress = result.total_pages
        job.status = JobStatus.COMPLETED
        _init_page_statuses(job, result.total_pages)
        for ps in job.page_statuses:
            ps.status = PageStatus.COMPLETED
        _append_log(job, "Đã nạp dữ liệu từ Excel", LogLevel.SUCCESS)
    return result


def _save_partial_result(job: JobInfo, pages: list[PageResult]) -> OcrResult:
    """Persist incremental OCR result after each page completes."""
    is_complete = job.status == JobStatus.COMPLETED
    result = OcrResult(
        job_id=job.job_id,
        filename=job.filename,
        total_pages=job.total_pages,
        pages=sorted(pages, key=lambda p: p.page_number),
        is_complete=is_complete,
        created_at=job.created_at,
        updated_at=datetime.now(),
    )
    _results[job.job_id] = result
    _save_result_to_disk(result)
    return result


def process_job(
    job_id: str,
    pdf_path: str | Path,
    processing_mode: ProcessingMode = ProcessingMode.LOCAL,
    api_provider: str = "",
    use_gpu: bool = False,
) -> OcrResult:
    """
    Process a PDF through the full OCR pipeline.

    Pages are processed sequentially; each completed page is saved
    immediately so the client can review while remaining pages run.
    """
    job = _jobs.get(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    try:
        job.status = JobStatus.PROCESSING
        job.use_gpu = use_gpu
        job.updated_at = datetime.now()

        device_label = "GPU" if use_gpu else "CPU"
        worker_label = os.environ.get("OCR_WORKER_LABEL", "").strip()
        if worker_label:
            _append_log(
                job,
                f"[{worker_label}] Worker bắt đầu OCR trên {device_label}",
            )
        else:
            _append_log(
                job,
                f"Bắt đầu xử lý — chế độ {processing_mode.value.upper()}, thiết bị {device_label}",
            )

        if processing_mode == ProcessingMode.LOCAL:
            if use_gpu:
                gpu_ok, gpu_detail = probe_local_gpu()
                if not gpu_ok:
                    _append_log(
                        job,
                        f"GPU máy local không dùng được ({gpu_detail}) — chạy CPU",
                        LogLevel.WARNING,
                    )
                    use_gpu = False
                    job.use_gpu = False
                    force_cpu_mode(gpu_detail)
                else:
                    _append_log(
                        job,
                        f"GPU sẵn sàng — {gpu_detail}",
                        LogLevel.SUCCESS,
                    )
            configure_ocr_device(use_gpu)
            if worker_label:
                _append_log(
                    job,
                    f"[{worker_label}] Engine OCR đã sẵn sàng ({device_label})",
                )
            else:
                _append_log(job, f"OCR engine đã cấu hình chạy trên {device_label}")

        # Step 1: Convert PDF to images
        _append_log(job, "Đang chuyển PDF sang ảnh (Poppler)…")
        image_paths = convert_pdf_to_images(pdf_path, job_id)
        job.total_pages = len(image_paths)
        _init_page_statuses(job, len(image_paths))
        _append_log(job, f"Đã tách {len(image_paths)} trang PDF thành ảnh PNG")

        # Step 2: OCR each page independently
        pages: list[PageResult] = []
        provider = api_provider or settings.ocr_api_provider

        for i, image_path in enumerate(image_paths, start=1):
            _set_page_status(job, i, PageStatus.PROCESSING)
            _append_log(job, f"▶ Bắt đầu OCR trang {i}/{len(image_paths)}…")

            try:
                if processing_mode == ProcessingMode.API:
                    page_result = process_page_via_api(
                        image_path=image_path,
                        page_number=i,
                        provider=provider,
                    )
                elif processing_mode == ProcessingMode.AUTO:
                    try:
                        page_result = process_page(
                            image_path, page_number=i, use_gpu=use_gpu
                        )
                    except Exception as local_error:
                        _append_log(
                            job,
                            f"Local OCR trang {i} lỗi, chuyển sang API: {local_error}",
                            LogLevel.WARNING,
                        )
                        page_result = process_page_via_api(
                            image_path=image_path,
                            page_number=i,
                            provider=provider,
                        )
                else:
                    try:
                        page_result = process_page(
                            image_path, page_number=i, use_gpu=use_gpu
                        )
                    except Exception as page_error:
                        if use_gpu and is_gpu_runtime_error(page_error):
                            _append_log(
                                job,
                                "GPU Paddle lỗi — thử lại trang này trên CPU…",
                                LogLevel.WARNING,
                            )
                            force_cpu_mode(page_error)
                            use_gpu = False
                            job.use_gpu = False
                            page_result = process_page(
                                image_path, page_number=i, use_gpu=False
                            )
                        else:
                            raise

                pages.append(page_result)
                _set_page_status(job, i, PageStatus.COMPLETED)
                job.progress = i
                job.updated_at = datetime.now()

                table_count = len(page_result.tables)
                cell_count = sum(len(t.cells) for t in page_result.tables)
                _append_log(
                    job,
                    f"✓ Trang {i}/{len(image_paths)} hoàn tất — "
                    f"{table_count} bảng, {cell_count} ô",
                    LogLevel.SUCCESS,
                )

                _save_partial_result(job, pages)

            except Exception as page_error:
                _set_page_status(job, i, PageStatus.FAILED, str(page_error))
                _append_log(
                    job,
                    f"✗ Trang {i} thất bại: {page_error}",
                    LogLevel.ERROR,
                )
                raise

        result = _save_partial_result(job, pages)
        job.status = JobStatus.COMPLETED
        job.updated_at = datetime.now()
        result.is_complete = True
        _results[job_id] = result
        _save_result_to_disk(result)

        _append_log(
            job,
            f"Hoàn tất toàn bộ {len(pages)} trang!",
            LogLevel.SUCCESS,
        )
        logger.info("[%s] Job completed successfully", job_id)
        return result

    except Exception as e:
        logger.error("[%s] Job failed: %s", job_id, e)
        job.status = JobStatus.FAILED
        job.error_message = str(e)
        job.updated_at = datetime.now()
        _append_log(job, f"Lỗi xử lý: {e}", LogLevel.ERROR)
        raise


def process_remote_job(
    job_id: str,
    pdf_path: str | Path,
    remote_provider: RemoteProvider,
    remote_url: str = "",
    use_gpu: bool = True,
    remote_token: str = "",
) -> OcrResult:
    """
    Forward PDF to a remote OCR worker and mirror progress locally.

    Used for internal GPU servers and Colab tunnel workers.
    """
    import time

    job = _jobs.get(job_id)
    if not job:
        raise ValueError(f"Job not found: {job_id}")

    pdf_path = Path(pdf_path)
    worker_url, worker_token = resolve_remote_target(
        remote_provider, remote_url, remote_token
    )
    label = provider_label(remote_provider)

    try:
        job.status = JobStatus.PROCESSING
        job.use_gpu = use_gpu
        job.remote_provider = remote_provider
        job.remote_url = worker_url
        job.updated_at = datetime.now()

        _append_log(
            job,
            f"Bắt đầu xử lý REMOTE — {label} @ {worker_url}",
        )
        _append_log(job, "Đang upload PDF lên worker GPU…")

        remote_job_id = upload_pdf_to_remote(
            pdf_path,
            worker_url,
            token=worker_token,
            use_gpu=use_gpu,
        )
        job.remote_job_id = remote_job_id
        _append_log(
            job,
            f"Worker đã nhận job {remote_job_id} — đang poll tiến trình…",
            LogLevel.SUCCESS,
        )

        last_cached_pages: set[int] = set()
        interval = settings.remote_poll_interval_seconds
        poll_errors = 0
        max_poll_errors = 15

        while True:
            try:
                remote_job = fetch_remote_status(
                    worker_url, remote_job_id, worker_token
                )
                mirror_remote_status(job, remote_job)
                job.updated_at = datetime.now()
                poll_errors = 0

                remote_result = fetch_remote_result(
                    worker_url, remote_job_id, worker_token
                )
            except Exception as poll_err:
                if is_transient_remote_error(poll_err) and poll_errors < max_poll_errors:
                    poll_errors += 1
                    _append_log(
                        job,
                        f"Kết nối {label} tạm gián đoạn — thử lại "
                        f"({poll_errors}/{max_poll_errors})…",
                        LogLevel.WARNING,
                    )
                    time.sleep(interval * 2)
                    continue
                raise

            if remote_result and remote_result.pages:
                local_result = OcrResult(
                    job_id=job.job_id,
                    filename=job.filename,
                    total_pages=remote_result.total_pages or job.total_pages,
                    pages=remote_result.pages,
                    is_complete=remote_job.status == JobStatus.COMPLETED,
                    created_at=job.created_at,
                    updated_at=datetime.now(),
                )
                _results[job_id] = local_result
                _save_result_to_disk(local_result)

                for page in remote_result.pages:
                    pn = page.page_number
                    if pn not in last_cached_pages:
                        try:
                            cache_remote_page_image(
                                job.job_id,
                                worker_url,
                                remote_job_id,
                                pn,
                                worker_token,
                            )
                            last_cached_pages.add(pn)
                        except Exception as img_err:
                            _append_log(
                                job,
                                f"Cảnh báo: chưa cache ảnh trang {pn}: {img_err}",
                                LogLevel.WARNING,
                            )

            if remote_job.status == JobStatus.COMPLETED:
                if remote_result is None:
                    remote_result = fetch_remote_result(
                        worker_url, remote_job_id, worker_token
                    )
                if remote_result is None:
                    raise RuntimeError("Worker hoàn tất nhưng không có kết quả")

                for pn in range(1, (remote_result.total_pages or 0) + 1):
                    if pn not in last_cached_pages:
                        cache_remote_page_image(
                            job.job_id,
                            worker_url,
                            remote_job_id,
                            pn,
                            worker_token,
                        )

                result = OcrResult(
                    job_id=job.job_id,
                    filename=job.filename,
                    total_pages=remote_result.total_pages,
                    pages=remote_result.pages,
                    is_complete=True,
                    created_at=job.created_at,
                    updated_at=datetime.now(),
                )
                job.status = JobStatus.COMPLETED
                job.progress = remote_job.progress
                job.total_pages = remote_result.total_pages
                job.updated_at = datetime.now()
                _results[job_id] = result
                _save_result_to_disk(result)
                _append_log(
                    job,
                    f"Hoàn tất remote OCR — {len(result.pages)} trang",
                    LogLevel.SUCCESS,
                )
                return result

            if remote_job.status == JobStatus.FAILED:
                raise RuntimeError(
                    remote_job.error_message or "Remote worker báo job thất bại"
                )

            time.sleep(interval)

    except Exception as e:
        logger.error("[%s] Remote job failed: %s", job_id, e)
        job.status = JobStatus.FAILED
        job.error_message = str(e)
        job.updated_at = datetime.now()
        _append_log(job, f"Lỗi remote OCR: {e}", LogLevel.ERROR)
        raise


def update_result(job_id: str, updates: list[UpdateCellRequest]) -> OcrResult:
    """
    Update OCR result cells after user review.

    Only pages that have been OCR'd can be updated.
    """
    result = get_result(job_id)
    if result is None:
        raise ValueError(f"Result not found for job: {job_id}")

    for update in updates:
        for page in result.pages:
            if page.page_number != update.page_number:
                continue
            for table in page.tables:
                if table.table_index != update.table_index:
                    continue
                for cell in table.cells:
                    if cell.row == update.row and cell.col == update.col:
                        cell.text = update.text
                        cell.confidence = 1.0
                        break

    result.updated_at = datetime.now()
    _results[job_id] = result
    _save_result_to_disk(result)

    job = get_job(job_id)
    if job and job.remote_job_id and job.remote_url:
        from app.services.remote_ocr_service import push_remote_updates

        token = get_job_remote_token(job_id)
        if not token and job.remote_provider == RemoteProvider.INTERNAL:
            _, token = resolve_remote_target(RemoteProvider.INTERNAL, job.remote_url)
        try:
            push_remote_updates(job.remote_url, job.remote_job_id, updates, token)
        except Exception as exc:
            logger.warning("[%s] Remote update forward failed: %s", job_id, exc)

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
