"""
OCR Service Configuration.
Đọc cấu hình từ .env file hoặc environment variables.
"""

from pathlib import Path
from urllib.parse import urlparse

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
    pdf_dpi: int = 250
    poppler_path: str = ""
    poppler_thread_count: int = 4
    pdf_lazy_convert: bool = True
    # Prefetch + Paddle GPU trên Windows dễ treo pdftoppm — mặc định tắt.
    pdf_prefetch_pages: bool = False
    pdf_convert_timeout_seconds: int = 120

    # --- Job queue (multi-user GPU host) ---
    ocr_queue_max_size: int = 30
    ocr_worker_threads: int = 2
    ocr_warmup_on_startup: bool = True
    ocr_save_every_n_pages: int = 1
    ocr_page_pipeline: bool = True

    # --- VietOCR / CPU tuning ---
    vietocr_batch_size: int = 32
    torch_num_threads: int = 4
    cell_ink_min_ratio: float = 0.015
    # Phase 4: VietOCR torch-CUDA trong process con (tránh xung đột Paddle GPU)
    vietocr_gpu_subprocess: bool = True
    vietocr_gpu_python: str = ""

    # --- Confidence ---
    ocr_confidence_threshold: float = 0.85

    # --- CORS ---
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # --- Remote OCR worker (internal GPU server / Colab tunnel) ---
    internal_gpu_url: str = ""
    internal_gpu_token: str = ""
    remote_worker_token: str = ""
    remote_poll_interval_seconds: float = 1.5
    remote_request_timeout_seconds: int = 120

    # --- Logging ---
    log_level: str = "INFO"

    # --- Keycloak (User Provisioning) ---
    keycloak_base_url: str = ""  # vd: https://keycloak-domain (không có dấu / cuối)
    keycloak_realm: str = ""
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""
    keycloak_verify_ssl: bool = True
    keycloak_timeout_seconds: int = 30
    keycloak_token_leeway_seconds: int = 30

    # Mặc định nghiệp vụ khi tạo/reset user
    keycloak_default_temporary: bool = True
    keycloak_default_required_actions: str = "UPDATE_PASSWORD,CONFIGURE_TOTP"
    # Mật khẩu tạm mặc định khi input không cung cấp password
    keycloak_default_temp_password: str = ""

    # Map tiêu đề cột (Excel/OCR, chữ thường không dấu cách thừa) -> field Keycloak.
    # Định dạng: "field:alias1|alias2;field2:aliasA|aliasB"
    keycloak_header_map: str = (
        "username:tên đăng nhập|tendangnhap|username|user|tài khoản|tai khoan;"
        "email:email|thư điện tử|thu dien tu;"
        "first_name:tên|first name|firstname;"
        "last_name:họ|họ và tên|last name|lastname;"
        "password:mật khẩu|mat khau|password|matkhau"
    )

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

    def resolve_internal_gpu_url(self, *, local_gpu_ok: bool | None = None) -> str:
        """
        URL worker GPU nội bộ.

        - .env INTERNAL_GPU_URL: dùng khi client proxy sang máy GPU khác
        - Máy host GPU (PADDLE_USE_GPU=true): tự trỏ localhost nếu chưa cấu hình
        """
        explicit = self.internal_gpu_url.strip()
        if explicit:
            return explicit
        if not self.paddle_use_gpu:
            return ""
        if local_gpu_ok is False:
            return ""
        return f"http://127.0.0.1:{self.port}"

    def is_local_worker_url(self, url: str) -> bool:
        """True when worker URL points to this same ocr-service instance."""
        raw = (url or "").strip()
        if not raw:
            return False
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return False
        port = parsed.port or 80
        return port == self.port
    @property
    def keycloak_default_required_actions_list(self) -> list[str]:
        """Parse default required actions from comma-separated string."""
        return [
            a.strip()
            for a in self.keycloak_default_required_actions.split(",")
            if a.strip()
        ]

    @property
    def keycloak_configured(self) -> bool:
        """True when all mandatory Keycloak settings are present."""
        return bool(
            self.keycloak_base_url.strip()
            and self.keycloak_realm.strip()
            and self.keycloak_client_id.strip()
            and self.keycloak_client_secret.strip()
        )

    @property
    def keycloak_header_map_parsed(self) -> dict[str, list[str]]:
        """
        Parse header map config into {field: [alias, ...]}.

        Aliases are lower-cased and stripped for case-insensitive matching.
        """
        result: dict[str, list[str]] = {}
        for group in self.keycloak_header_map.split(";"):
            group = group.strip()
            if not group or ":" not in group:
                continue
            field, aliases = group.split(":", 1)
            field = field.strip()
            alias_list = [a.strip().lower() for a in aliases.split("|") if a.strip()]
            if field and alias_list:
                result[field] = alias_list
        return result


settings = Settings()
