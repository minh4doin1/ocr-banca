"""
OCR Service — FastAPI Application.

Main entry point for the Vietnamese OCR service that extracts
table data from PDF files for the Banca user batch creation system.
"""

from __future__ import annotations

import logging
import os
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers.ocr import router as ocr_router
from app.routers.users import router as users_router
from app.services.gpu_runtime import probe_gpu_runtime
from app.services.pdf_service import check_poppler_available

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


_kc_file_logging_ready = False


def _setup_keycloak_logging() -> None:
    """Ghi log Keycloak chi tiết ra logs/keycloak.log khi KEYCLOAK_DEBUG=true."""
    global _kc_file_logging_ready
    if _kc_file_logging_ready or not settings.keycloak_debug:
        return
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = logging.FileHandler(log_dir / "keycloak.log", encoding="utf-8")
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for name in (
        "app.services.keycloak_service",
        "app.routers.users",
        "app.services.keycloak_diagnostics",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(fh)
    _kc_file_logging_ready = True
    logger.info("Keycloak debug logging → %s", log_dir / "keycloak.log")


_setup_keycloak_logging()

# ──────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Banca OCR Service",
    description=(
        "Dịch vụ OCR tiếng Việt — Trích xuất bảng dữ liệu từ file PDF "
        "phục vụ tạo lô user cho hệ thống Bancassurance Agribank.\n\n"
        "**Engine:** PaddleOCR PP-Structure + VietOCR Transformer"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ──────────────────────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "X-OCR-Target-Env", "Authorization", "Content-Type"],
)

# ──────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────

import os
from fastapi.staticfiles import StaticFiles

app.include_router(ocr_router)
app.include_router(users_router)


def _vietocr_gpu_health() -> bool:
    if not settings.vietocr_gpu_subprocess or not settings.paddle_use_gpu:
        return False
    try:
        from app.services.vietocr_gpu_client import get_vietocr_gpu_client

        client = get_vietocr_gpu_client(auto_start=False)
        return client is not None and client.is_ready
    except Exception:
        return False


@app.get("/health", tags=["Health"])
async def health_check():
    """Detailed health check."""
    poppler_ok, poppler_info = check_poppler_available()
    gpu_status = probe_gpu_runtime()
    healthy = poppler_ok and (
        not settings.paddle_use_gpu or gpu_status.paddle_gpu_ok
    )
    payload = {
        "status": "healthy" if healthy else "degraded",
        "poppler_ok": poppler_ok,
        "poppler": poppler_info,
        "gpu": gpu_status.to_dict(),
        "engine": settings.ocr_engine,
        "vietocr_model": settings.vietocr_model,
        "pdf_dpi": settings.pdf_dpi,
        "confidence_threshold": settings.ocr_confidence_threshold,
        "gpu_enabled": settings.paddle_use_gpu,
        "gpu_available": gpu_status.paddle_gpu_ok,
        "vietocr_gpu_subprocess": settings.vietocr_gpu_subprocess,
        "vietocr_gpu_ready": _vietocr_gpu_health(),
        "internal_gpu_configured": bool(
            settings.resolve_internal_gpu_url(local_gpu_ok=gpu_status.paddle_gpu_ok)
        ),
        "worker_token_required": bool(settings.remote_worker_token.strip()),
        "role": "worker" if settings.paddle_use_gpu else "client-or-cpu",
    }
    try:
        from app.services.job_queue import get_job_queue

        payload["ocr_queue"] = get_job_queue().stats()
    except Exception:
        payload["ocr_queue"] = {"queue_depth": 0}
    return payload


# Mount standalone frontend static files at root
frontend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../ocr-fe"))
if os.path.exists(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at: %s", frontend_dir)



# ──────────────────────────────────────────────────────────────
# Startup / Shutdown
# ──────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup_event():
    """Application startup: ensure storage directories exist."""
    from app.routers.ocr import init_ocr_worker
    from app.services.ocr_service import warmup_ocr_engines

    logger.info("=" * 60)
    logger.info("Banca OCR Service starting up")
    logger.info("APP_ENV: %s", settings.app_env)
    logger.info("Engine: %s", settings.ocr_engine)
    logger.info("GPU: %s", settings.paddle_use_gpu)
    gpu = probe_gpu_runtime()
    if gpu.nvidia_detected:
        logger.info("GPU card: %s | Paddle GPU OK: %s", gpu.gpu_name, gpu.paddle_gpu_ok)
        if settings.paddle_use_gpu and not gpu.paddle_gpu_ok:
            logger.warning("PADDLE_USE_GPU=true nhưng GPU chưa sẵn sàng: %s", gpu.detail)
    logger.info("DPI: %d (lazy=%s)", settings.pdf_dpi, settings.pdf_lazy_convert)
    logger.info(
        "Queue: max=%d workers=%d",
        settings.ocr_queue_max_size,
        settings.ocr_worker_threads,
    )
    logger.info("Storage: %s", settings.storage_path)
    logger.info("CORS: %s", settings.cors_origins_list)
    logger.info("=" * 60)

    # Create storage directories
    _ = settings.upload_path
    _ = settings.result_path
    _ = settings.export_path
    _ = settings.images_path

    init_ocr_worker()

    if settings.ocr_warmup_on_startup:
        threading.Thread(
            target=warmup_ocr_engines,
            name="ocr-warmup",
            daemon=True,
        ).start()


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown."""
    from app.services.vietocr_gpu_client import shutdown_vietocr_gpu_client

    shutdown_vietocr_gpu_client()
    logger.info("Banca OCR Service shutting down")
