"""
OCR Service — FastAPI Application.

Main entry point for the Vietnamese OCR service that extracts
table data from PDF files for the Banca user batch creation system.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers.ocr import router as ocr_router
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
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────

import os
from fastapi.staticfiles import StaticFiles

app.include_router(ocr_router)


@app.get("/health", tags=["Health"])
async def health_check():
    """Detailed health check."""
    poppler_ok, poppler_info = check_poppler_available()
    gpu_status = probe_gpu_runtime()
    healthy = poppler_ok and (
        not settings.paddle_use_gpu or gpu_status.paddle_gpu_ok
    )
    return {
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
        "internal_gpu_configured": bool(settings.internal_gpu_url.strip()),
        "worker_token_required": bool(settings.remote_worker_token.strip()),
        "role": "worker" if settings.paddle_use_gpu else "client-or-cpu",
    }


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
    logger.info("=" * 60)
    logger.info("Banca OCR Service starting up")
    logger.info("Engine: %s", settings.ocr_engine)
    logger.info("GPU: %s", settings.paddle_use_gpu)
    gpu = probe_gpu_runtime()
    if gpu.nvidia_detected:
        logger.info("GPU card: %s | Paddle GPU OK: %s", gpu.gpu_name, gpu.paddle_gpu_ok)
        if settings.paddle_use_gpu and not gpu.paddle_gpu_ok:
            logger.warning("PADDLE_USE_GPU=true nhưng GPU chưa sẵn sàng: %s", gpu.detail)
    logger.info("DPI: %d", settings.pdf_dpi)
    logger.info("Storage: %s", settings.storage_path)
    logger.info("CORS: %s", settings.cors_origins_list)
    logger.info("=" * 60)

    # Create storage directories
    _ = settings.upload_path
    _ = settings.result_path
    _ = settings.export_path
    _ = settings.images_path


@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown."""
    logger.info("Banca OCR Service shutting down")
