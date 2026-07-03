"""
Remote OCR Service — Proxy client for internal GPU server or Colab worker.

Local ocr-service forwards PDF jobs to a remote ocr-service instance
and mirrors status/logs/results back to the local job store.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from app.config import settings
from app.models.schemas import (
    JobInfo,
    JobLogEntry,
    JobStatus,
    LogLevel,
    OcrResult,
    RemoteProvider,
    RemoteWorkerHealth,
    UpdateCellRequest,
)

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def _auth_headers(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _response_json(res: requests.Response, context: str) -> dict:
    """Parse JSON body; raise a clear error when tunnel/worker returns HTML or empty body."""
    if not res.content or not res.content.strip():
        raise RuntimeError(
            f"{context}: worker trả về rỗng (HTTP {res.status_code}). "
            "Kiểm tra tunnel Colab còn sống và notebook vẫn đang chạy."
        )
    try:
        data = res.json()
    except ValueError as exc:
        snippet = (res.text or "").strip().replace("\n", " ")[:160]
        raise RuntimeError(
            f"{context}: phản hồi không phải JSON (HTTP {res.status_code}): "
            f"{snippet or '(rỗng)'}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"{context}: JSON không hợp lệ (HTTP {res.status_code})"
        )
    return data


def _response_detail(res: requests.Response, context: str) -> str:
    """Extract error detail from a failed HTTP response."""
    if not res.content or not res.content.strip():
        return f"HTTP {res.status_code} — phản hồi rỗng (tunnel Colab có thể đã ngắt)"
    try:
        data = res.json()
        if isinstance(data, dict):
            return str(data.get("detail", res.text))
    except ValueError:
        pass
    return (res.text or f"HTTP {res.status_code}")[:300]


def _request_with_retry(
    method: str,
    url: str,
    *,
    context: str,
    token: str = "",
    retries: int = 3,
    **kwargs,
) -> requests.Response:
    """HTTP call with short retries for flaky Colab/cloudflared tunnels."""
    last_error: Exception | None = None
    headers = {**kwargs.pop("headers", {}), **_auth_headers(token)}
    for attempt in range(1, retries + 1):
        try:
            res = requests.request(method, url, headers=headers, **kwargs)
            if res.status_code in (502, 503, 504) and attempt < retries:
                logger.warning(
                    "%s: HTTP %s — retry %d/%d",
                    context,
                    res.status_code,
                    attempt,
                    retries,
                )
                continue
            return res
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                logger.warning("%s: %s — retry %d/%d", context, exc, attempt, retries)
                continue
            raise RuntimeError(f"{context}: {exc}") from exc
    if last_error:
        raise RuntimeError(f"{context}: {last_error}") from last_error
    raise RuntimeError(f"{context}: request failed")


def resolve_remote_target(
    provider: RemoteProvider,
    remote_url: str = "",
    remote_token: str = "",
) -> tuple[str, str]:
    """
    Resolve remote worker URL and token from provider + config.

    Returns:
        (url, token)
    """
    if provider == RemoteProvider.INTERNAL:
        url = remote_url or settings.internal_gpu_url
        token = settings.internal_gpu_token
        if not url:
            raise ValueError(
                "Chưa cấu hình INTERNAL_GPU_URL trong .env cho worker GPU nội bộ"
            )
        return _normalize_url(url), token

    url = remote_url.strip()
    if not url:
        raise ValueError("Vui lòng nhập URL tunnel Colab/worker")
    return _normalize_url(url), remote_token.strip()


def check_worker_health(
    url: str,
    token: str = "",
    timeout: int = 10,
) -> RemoteWorkerHealth:
    """Ping remote worker /health endpoint."""
    url = _normalize_url(url)
    try:
        res = requests.get(
            f"{url}/health",
            headers=_auth_headers(token),
            timeout=timeout,
        )
        if res.status_code == 401:
            return RemoteWorkerHealth(
                url=url,
                reachable=True,
                status="unauthorized",
                detail="Worker yêu cầu token — kiểm tra REMOTE_WORKER_TOKEN",
            )
        if not res.ok:
            return RemoteWorkerHealth(
                url=url,
                reachable=False,
                status="error",
                detail=f"HTTP {res.status_code}",
            )
        try:
            data = _response_json(res, "Health check")
        except RuntimeError as exc:
            return RemoteWorkerHealth(
                url=url,
                reachable=False,
                status="error",
                detail=str(exc),
            )
        return RemoteWorkerHealth(
            url=url,
            reachable=True,
            status=data.get("status", "healthy"),
            detail="OK",
            use_gpu=data.get("gpu_enabled"),
        )
    except requests.RequestException as exc:
        return RemoteWorkerHealth(
            url=url,
            reachable=False,
            status="offline",
            detail=str(exc),
        )


def upload_pdf_to_remote(
    pdf_path: Path,
    remote_url: str,
    token: str = "",
    use_gpu: bool = True,
) -> str:
    """Upload PDF to remote worker; return remote job_id."""
    url = _normalize_url(remote_url)
    with pdf_path.open("rb") as pdf_file:
        res = _request_with_retry(
            "POST",
            f"{url}/api/ocr/upload",
            context="Upload PDF lên worker",
            token=token,
            files={"file": (pdf_path.name, pdf_file, "application/pdf")},
            data={
                "processing_mode": "local",
                "use_gpu": "true" if use_gpu else "false",
            },
            timeout=settings.remote_request_timeout_seconds,
            retries=2,
        )
    if res.status_code == 401:
        raise PermissionError("Remote worker từ chối token xác thực")
    if not res.ok:
        raise RuntimeError(
            f"Remote upload failed: {_response_detail(res, 'Upload PDF lên worker')}"
        )
    data = _response_json(res, "Upload PDF lên worker")
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError("Worker không trả về job_id sau upload")
    return str(job_id)


def fetch_remote_status(
    remote_url: str,
    remote_job_id: str,
    token: str = "",
) -> JobInfo:
    """Fetch job status from remote worker."""
    url = _normalize_url(remote_url)
    res = _request_with_retry(
        "GET",
        f"{url}/api/ocr/status/{remote_job_id}",
        context="Lấy trạng thái worker",
        token=token,
        timeout=settings.remote_request_timeout_seconds,
    )
    if not res.ok:
        raise RuntimeError(
            f"Remote status failed: {_response_detail(res, 'Lấy trạng thái worker')}"
        )
    return JobInfo(**_response_json(res, "Lấy trạng thái worker"))


def fetch_remote_result(
    remote_url: str,
    remote_job_id: str,
    token: str = "",
) -> OcrResult | None:
    """Fetch OCR result from remote; None if still processing (HTTP 202)."""
    url = _normalize_url(remote_url)
    res = _request_with_retry(
        "GET",
        f"{url}/api/ocr/result/{remote_job_id}",
        context="Lấy kết quả worker",
        token=token,
        timeout=settings.remote_request_timeout_seconds,
    )
    if res.status_code == 202:
        return None
    if res.status_code == 500:
        raise RuntimeError(
            _response_detail(res, "Worker báo job thất bại")
        )
    if not res.ok:
        raise RuntimeError(
            f"Remote result failed: {_response_detail(res, 'Lấy kết quả worker')}"
        )
    data = _response_json(res, "Lấy kết quả worker")
    data["job_id"] = data.get("job_id", remote_job_id)
    return OcrResult(**data)


def push_remote_updates(
    remote_url: str,
    remote_job_id: str,
    updates: list[UpdateCellRequest],
    token: str = "",
) -> None:
    """Forward cell updates to remote worker (best-effort)."""
    if not updates:
        return
    url = _normalize_url(remote_url)
    payload = {"updates": [u.model_dump() for u in updates]}
    try:
        requests.put(
            f"{url}/api/ocr/result/{remote_job_id}",
            headers={**_auth_headers(token), "Content-Type": "application/json"},
            json=payload,
            timeout=settings.remote_request_timeout_seconds,
        )
    except requests.RequestException as exc:
        logger.warning("Remote update sync failed: %s", exc)


def cache_remote_page_image(
    local_job_id: str,
    remote_url: str,
    remote_job_id: str,
    page_number: int,
    token: str = "",
) -> Path:
    """Download page image from remote worker and cache locally."""
    local_dir = settings.images_path / local_job_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / f"page_{page_number:03d}.png"
    if local_path.exists():
        return local_path

    url = _normalize_url(remote_url)
    res = requests.get(
        f"{url}/api/ocr/result/{remote_job_id}/page/{page_number}/image",
        headers=_auth_headers(token),
        timeout=settings.remote_request_timeout_seconds,
    )
    if not res.ok:
        raise RuntimeError(
            f"Không tải được ảnh trang {page_number} từ remote: HTTP {res.status_code}"
        )
    local_path.write_bytes(res.content)
    return local_path


def _format_remote_log_message(message: str, provider_label: str) -> str:
    """Prefix and clarify worker logs mirrored to the local client."""
    if not provider_label:
        return message

    rewritten = message
    if "chế độ LOCAL" in rewritten:
        rewritten = rewritten.replace(
            "chế độ LOCAL",
            f"worker {provider_label}",
        )
    if rewritten.startswith(f"[{provider_label}]"):
        return rewritten
    return f"[{provider_label}] {rewritten}"


def sync_logs_from_remote(local_job: JobInfo, remote_job: JobInfo) -> None:
    """Append new log lines from remote job onto local job."""
    label = ""
    if local_job.remote_provider:
        label = provider_label(local_job.remote_provider)

    existing = {(e.timestamp, e.message) for e in local_job.logs}
    for entry in remote_job.logs:
        formatted = _format_remote_log_message(entry.message, label)
        key = (entry.timestamp, formatted)
        if key not in existing:
            local_job.logs.append(
                JobLogEntry(
                    timestamp=entry.timestamp,
                    message=formatted,
                    level=entry.level,
                )
            )
            existing.add(key)
    if len(local_job.logs) > 300:
        local_job.logs = local_job.logs[-300:]


def mirror_remote_status(local_job: JobInfo, remote_job: JobInfo) -> None:
    """Copy progress fields from remote status onto local job."""
    sync_logs_from_remote(local_job, remote_job)
    local_job.progress = remote_job.progress
    local_job.total_pages = remote_job.total_pages or local_job.total_pages
    local_job.page_statuses = remote_job.page_statuses
    local_job.error_message = remote_job.error_message


def provider_label(provider: RemoteProvider) -> str:
    labels = {
        RemoteProvider.INTERNAL: "GPU nội bộ",
        RemoteProvider.COLAB: "Google Colab",
        RemoteProvider.CUSTOM: "Worker tùy chỉnh",
    }
    return labels.get(provider, provider.value)


def is_transient_remote_error(error: Exception) -> bool:
    """True when Colab tunnel / network may recover on retry."""
    text = str(error).lower()
    markers = (
        "expecting value",
        "phản hồi rỗng",
        "không phải json",
        "connection",
        "timeout",
        "timed out",
        "502",
        "503",
        "504",
        "tunnel",
        "remote end closed",
        "broken pipe",
    )
    return any(marker in text for marker in markers)
