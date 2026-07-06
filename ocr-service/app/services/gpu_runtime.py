"""
GPU runtime helpers for Windows/Linux OCR host (NVIDIA RTX 2070, etc.).

Configures PATH so Paddle can find CUDA/cuDNN from:
  1. nvidia-* pip packages (recommended, no manual CUDA install)
  2. CUDA Toolkit in Program Files
  3. ocr-service/bin/cuda (manual cudnn drop-in)
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_GPU_INFERENCE_LOCK = threading.Lock()
_ACTIVE_OCR_JOBS = 0

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BIN_CUDA = _PROJECT_ROOT / "bin" / "cuda"


@dataclass
class GpuRuntimeStatus:
    """GPU readiness report for /health and startup logs."""

    nvidia_detected: bool = False
    gpu_name: str = ""
    driver_cuda: str = ""
    cudnn_found: bool = False
    cudnn_path: str = ""
    cuda_paths_added: list[str] | None = None
    paddle_gpu_ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "nvidia_detected": self.nvidia_detected,
            "gpu_name": self.gpu_name,
            "driver_cuda": self.driver_cuda,
            "cudnn_found": self.cudnn_found,
            "cudnn_path": self.cudnn_path,
            "paddle_gpu_ok": self.paddle_gpu_ok,
            "detail": self.detail,
        }


_PROBE_CACHE: GpuRuntimeStatus | None = None
_PROBE_CACHE_AT: float = 0.0
_PROBE_TTL_SECONDS = 60.0


def _prepend_path(*dirs: str) -> None:
    """Prepend directories to PATH (process-local)."""
    existing = os.environ.get("PATH", "")
    parts = [d for d in dirs if d and Path(d).exists()]
    if not parts:
        return
    os.environ["PATH"] = ";".join(parts) + (";" + existing if existing else "")


def _find_cudnn_dll() -> Path | None:
    """Locate cudnn64_8.dll on disk."""
    which = shutil.which("cudnn64_8.dll")
    if which:
        return Path(which)

    search_roots: list[Path] = [_BIN_CUDA]
    if platform.system() == "Windows":
        toolkit = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
        if toolkit.exists():
            search_roots.extend(sorted(toolkit.iterdir(), reverse=True))

    try:
        import site

        for sp in site.getsitepackages():
            search_roots.append(Path(sp) / "nvidia" / "cudnn" / "bin")
            search_roots.append(Path(sp) / "nvidia" / "cublas" / "bin")
            search_roots.append(Path(sp) / "nvidia" / "cuda_runtime" / "bin")
            search_roots.append(Path(sp) / "nvidia" / "cuda_nvrtc" / "bin")
    except Exception:
        pass

    for root in search_roots:
        if not root.exists():
            continue
        candidate = root / "cudnn64_8.dll"
        if candidate.exists():
            return candidate
        for hit in root.rglob("cudnn64_8.dll"):
            return hit
    return None


def _nvidia_smi_info() -> tuple[bool, str, str]:
    """Return (detected, gpu_name, driver_cuda_version)."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if not out:
            return False, "", ""
        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        name = parts[0] if parts else ""
        driver = parts[1] if len(parts) > 1 else ""
        smi = subprocess.check_output(
            ["nvidia-smi"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
        cuda_ver = ""
        marker = "CUDA Version:"
        if marker in smi:
            cuda_ver = smi.split(marker, 1)[1].strip().split()[0]
        return True, name, cuda_ver
    except Exception:
        return False, "", ""


def setup_gpu_path() -> list[str]:
    """
    Add CUDA/cuDNN directories to PATH before Paddle import.

    Returns list of paths added.
    """
    added: list[str] = []

    # pip nvidia-* packages (Windows/Linux)
    try:
        import site

        for sp in site.getsitepackages():
            base = Path(sp) / "nvidia"
            for sub in ("cudnn/bin", "cublas/bin", "cuda_runtime/bin", "cuda_nvrtc/bin"):
                p = base / sub.replace("/", os.sep)
                if p.exists():
                    added.append(str(p.resolve()))
    except Exception:
        pass

    # Manual drop-in: ocr-service/bin/cuda
    if _BIN_CUDA.exists():
        added.append(str(_BIN_CUDA.resolve()))

    # CUDA Toolkit (if installed)
    toolkit = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA")
    if toolkit.exists():
        for ver_dir in sorted(toolkit.iterdir(), reverse=True):
            bin_dir = ver_dir / "bin"
            if bin_dir.exists():
                added.append(str(bin_dir.resolve()))
                break

    # Paddle bundled libs (avoid importing paddle here)
    try:
        import site

        for sp in site.getsitepackages():
            paddle_libs = Path(sp) / "paddle" / "libs"
            if paddle_libs.exists():
                added.append(str(paddle_libs.resolve()))
    except Exception:
        pass

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for p in added:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    if unique:
        _prepend_path(*unique)
        logger.info("GPU PATH configured: %s", unique[:4])

    return unique


def gpu_inference_lock():
    """Serialize Paddle GPU calls (health probe vs OCR job)."""
    return _GPU_INFERENCE_LOCK


def begin_ocr_job() -> None:
    global _ACTIVE_OCR_JOBS
    with _GPU_INFERENCE_LOCK:
        _ACTIVE_OCR_JOBS += 1


def end_ocr_job() -> None:
    global _ACTIVE_OCR_JOBS
    with _GPU_INFERENCE_LOCK:
        _ACTIVE_OCR_JOBS = max(0, _ACTIVE_OCR_JOBS - 1)


def probe_gpu_runtime(*, force: bool = False) -> GpuRuntimeStatus:
    """Full GPU readiness check (safe to call from /health). Cached ~60s."""
    global _PROBE_CACHE, _PROBE_CACHE_AT

    now = time.monotonic()
    if (
        not force
        and _PROBE_CACHE is not None
        and (now - _PROBE_CACHE_AT) < _PROBE_TTL_SECONDS
    ):
        return _PROBE_CACHE

    # Tránh probe GPU song song khi đang OCR (gây PreconditionNotMet trên Windows).
    if not force and _ACTIVE_OCR_JOBS > 0 and _PROBE_CACHE is not None:
        return _PROBE_CACHE

    with _GPU_INFERENCE_LOCK:
        status = _probe_gpu_runtime_uncached()
        _PROBE_CACHE = status
        _PROBE_CACHE_AT = time.monotonic()
        return status


def _probe_gpu_runtime_uncached() -> GpuRuntimeStatus:
    """Run GPU probe without cache."""
    status = GpuRuntimeStatus(cuda_paths_added=[])
    detected, name, cuda_ver = _nvidia_smi_info()
    status.nvidia_detected = detected
    status.gpu_name = name
    status.driver_cuda = cuda_ver

    if not detected:
        status.detail = "Không phát hiện GPU NVIDIA (nvidia-smi)"
        return status

    setup_gpu_path()
    cudnn = _find_cudnn_dll()
    status.cudnn_found = cudnn is not None
    status.cudnn_path = str(cudnn.parent) if cudnn else ""

    if not status.cudnn_found:
        status.detail = (
            "Thiếu cuDNN — chạy: pip install nvidia-cudnn-cu11 nvidia-cuda-runtime-cu11"
        )
        return status

    try:
        import paddle

        if not paddle.is_compiled_with_cuda():
            status.detail = "Paddle không build CUDA"
            return status
        paddle.device.set_device("gpu:0")
        t = paddle.to_tensor([1.0], dtype="float32")
        _ = t + 1
        status.paddle_gpu_ok = True
        status.detail = "OK"
    except Exception as exc:
        status.detail = str(exc)[:300]

    return status


def ensure_gpu_ready() -> GpuRuntimeStatus:
    """
    Setup PATH and verify GPU for OCR jobs.

    Raises RuntimeError when GPU requested but not usable.
    """
    status = probe_gpu_runtime()
    if not status.paddle_gpu_ok:
        raise RuntimeError(
            f"GPU không sẵn sàng: {status.detail}. "
            "Cài: pip install nvidia-cudnn-cu11 nvidia-cuda-runtime-cu11 nvidia-cublas-cu11"
        )
    return status
