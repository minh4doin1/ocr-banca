"""
OCR Service Configuration.
Đọc cấu hình từ .env file hoặc environment variables.
"""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8100

    # --- Storage ---
    storage_dir: str = "./storage"

    # --- OCR Engine ---
    ocr_engine: str = "paddle_vietocr"  # "paddle_vietocr" or "tesseract"

    # PaddleOCR
    paddle_use_gpu: bool = False
    paddle_lang: str = "vi"

    # VietOCR
    vietocr_model: str = "vgg_transformer"

    # OCR API fallback mode
    ocr_api_provider: str = "ocrspace"
    ocrspace_api_key: str = "helloworld"
    ocr_api_timeout_seconds: int = 60

    # --- PDF Processing ---
    pdf_dpi: int = 300
    poppler_path: str = ""

    # --- Confidence ---
    ocr_confidence_threshold: float = 0.85

    # --- CORS ---
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # --- Logging ---
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def storage_path(self) -> Path:
        """Get resolved storage directory path."""
        path = Path(self.storage_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def upload_path(self) -> Path:
        """Directory for uploaded PDF files."""
        path = self.storage_path / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def result_path(self) -> Path:
        """Directory for OCR result JSON files."""
        path = self.storage_path / "results"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def export_path(self) -> Path:
        """Directory for exported Excel files."""
        path = self.storage_path / "exports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def images_path(self) -> Path:
        """Directory for converted PDF page images."""
        path = self.storage_path / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        return [origin.strip() for origin in self.cors_origins.split(",")]


settings = Settings()
