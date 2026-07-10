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
    # Môi trường instance backend (dev|prod) — hiển thị trên FE
    app_env: str = "dev"
    # URL API OCR cho FE khi chọn DEV / PROD (prod để trống = cùng origin khi deploy chung)
    ocr_env_dev_api_url: str = "http://localhost:8100"
    ocr_env_prod_api_url: str = ""
    ocr_env_dev_label: str = "DEV"
    ocr_env_prod_label: str = "PROD"

    # --- Storage ---
    storage_dir: str = "./storage"

    # --- OCR Engine ---
    ocr_engine: str = "paddle_vietocr"  # "paddle_vietocr" or "tesseract"

    # PaddleOCR
    paddle_use_gpu: bool = False
    paddle_lang: str = "vi"

    # VietOCR
    vietocr_model: str = "vgg_seq2seq"
    vietocr_model_pass2: str = "vgg_transformer"

    # OCR API fallback mode
    ocr_api_provider: str = "ocrspace"
    ocrspace_api_key: str = "helloworld"
    ocr_api_timeout_seconds: int = 60

    # --- PDF Processing ---
    pdf_dpi: int = 350
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

    # --- SSO accuracy enhancements (Agribank form) ---
    ocr_sso_enhance: bool = True
    ocr_symbol_normalize: bool = True
    ocr_sso_row_merge: bool = True
    ocr_sso_grid_relax: bool = True
    # Tách nhiều dòng chữ trong cùng một ô grid trước khi VietOCR
    ocr_cell_multiline: bool = True
    # Gộp band grid nội bộ ô (tắt mặc định — dễ nuốt nhiều dòng dữ liệu)
    ocr_sso_collapse_row_bands: bool = False
    # Email SSO luôn @agribank.com.vn — chỉ OCR phần username (nhanh hơn)
    ocr_sso_email_fixed_domain: bool = True
    ocr_sso_email_domain: str = "@agribank.com.vn"
    # Cột email (0-based); -1 = tự đoán theo layout SSO (thường cột 5)
    ocr_sso_email_col: int = -1
    ocr_sso_email_ipcas_priority: bool = True
    ocr_sso_critical_col_upscale: float = 2.5
    ocr_sso_role_fuzzy_threshold: float = 0.72
    ocr_sso_pass2_enabled: bool = True

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
    # Ghi log chi tiết Keycloak vào logs/keycloak.log (mọi request + traceback provision)
    keycloak_debug: bool = False

    # --- Keycloak (User Provisioning) ---
    keycloak_base_url: str = ""  # vd: https://keycloak-domain (không có dấu / cuối)
    keycloak_realm: str = ""
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""
    keycloak_verify_ssl: bool = True
    keycloak_timeout_seconds: int = 30
    keycloak_token_leeway_seconds: int = 30

    # --- Keycloak Admin Proxy (bypass F5/WAF) ---
    # Khi set, KeycloakClient sẽ gọi qua proxy (kc-proxy pod trong cluster)
    # thay vì gọi trực tiếp admin-sso.agribank.com (bị F5 chặn).
    # Set base_url thành URL của proxy ingress (vd https://api.agribank.com.vn/api/v1/iam-bridge).
    keycloak_proxy_api_key: str = ""  # Shared secret với proxy; trống = không gửi header

    # Mặc định nghiệp vụ khi tạo/reset user
    keycloak_default_temporary: bool = True
    keycloak_default_required_actions: str = "UPDATE_PASSWORD,CONFIGURE_TOTP"
    # Mật khẩu tạm mặc định khi input không cung cấp password
    keycloak_default_temp_password: str = "Agribank@123"

    # Client chứa các role banca-* (public clientId, không phải UUID)
    keycloak_roles_client_id: str = ""
    # Client riêng để gán role (cần manage-users + manage-clients hoặc realm-admin).
    # Để trống = dùng KEYCLOAK_CLIENT_ID.
    keycloak_role_assign_client_id: str = ""
    keycloak_role_assign_client_secret: str = ""

    # Keycloak PROD — cùng server OCR, FE chuyển môi trường qua header X-OCR-Target-Env
    keycloak_prod_base_url: str = ""
    keycloak_prod_realm: str = ""
    keycloak_prod_client_id: str = ""
    keycloak_prod_client_secret: str = ""
    keycloak_prod_roles_client_id: str = ""
    # UUID client chứa role banca-* (bỏ qua GET /clients khi WAF chặn)
    keycloak_prod_roles_client_uuid: str = ""
    keycloak_prod_verify_ssl: bool = True
    keycloak_prod_role_assign_client_id: str = ""
    keycloak_prod_role_assign_client_secret: str = ""

    # Map vai trò nghiệp vụ / alias -> tên client role Keycloak
    keycloak_role_map: str = (
        "quản trị:banca-admin;admin:banca-admin;"
        "đại lý viên:banca-seller;dai ly vien:banca-seller;seller:banca-seller;"
        "kế toán viên:banca-accounting-operator;ke toan vien:banca-accounting-operator;"
        "phê duyệt viên:banca-accounting-controller;phe duyet vien:banca-accounting-controller;"
        "phe duyet:banca-accounting-controller;duyet vien:banca-accounting-controller;"
        "đại lí viên:banca-seller;dai li vien:banca-seller;dai li:banca-seller;"
        "kế toán:banca-accounting-operator;ke toan:banca-accounting-operator;"
        "kt vien:banca-accounting-operator;ketoan:banca-accounting-operator;"
        "banca-admin:banca-admin;banca-seller:banca-seller;"
        "banca-accounting-operator:banca-accounting-operator;"
        "banca-accounting-controller:banca-accounting-controller"
    )

    # Map field nội bộ -> Keycloak attribute key
    keycloak_attribute_map: str = (
        "cccd:cccd;name:fullName;branch_code:branchCode;agent_code:agentCode;"
        "branch_name:branchName;department_name:departmentName;"
        "ipcas_code:ipcasCode;phone:phoneNumber;unit_code:unitCode"
    )

    # Map tiêu đề cột (Excel/OCR, chữ thường không dấu cách thừa) -> field Keycloak.
    # Định dạng: "field:alias1|alias2;field2:aliasA|aliasB"
    keycloak_header_map: str = (
        "username:tên đăng nhập|tendangnhap|username|user|tài khoản|tai khoan;"
        "email:email|thư điện tử|thu dien tu|email agribank|email tại agribank|email tai agribank;"
        "name:họ tên|họ và tên|hoten|ho ten|ho va ten|full name|fullname;"
        "first_name:tên|first name|firstname;"
        "last_name:họ|last name|lastname;"
        "cccd:cccd|căn cước|can cuoc|cmnd|số cccd|so cccd;"
        "branch_name:chi nhánh|chi nhanh|tên chi nhánh|ten chi nhanh|cn|branch;"
        "department_name:phòng giao dịch|phong giao dich|pgd|phòng gd|phong gd|phòng/đơn vị|phong/don vi|phong / don vi;"
        "branch_code:mã chi nhánh|ma chi nhanh|mã cn|ma cn|branch code;"
        "agent_code:mã đại lý|ma dai ly|mã đl|ma dl|agent code|agency code;"
        "ipcas_code:mã ipcas|ma ipcas|ipcas|user ipcas|mã ipcas;"
        "phone:số điện thoại|so dien thoai|sdt|sđt|phone|điện thoại|dien thoai|sđt|so dt;"
        "unit_code:mã đơn vị|ma don vi|mã dv|ma dv|mã đv|ma dv|unit code|ghi chú|ghichu|ghi chu|ghi chú / mã đv|ghi chu / ma dv|mã liên ngân hàng|ma lien ngan hang|ma lien ngan hang;"
        "role:vai trò|vai tro|role|quyền|quyen|chức danh|chuc danh|phân quyền|phan quyen;"
        "password:mật khẩu|mat khau|password|matkhau"
    )

    field_labels_vi_map: str = (
        "email:Email;"
        "first_name:Tên;"
        "last_name:Họ;"
        "branch_code:Mã CN;"
        "ipcas_code:IPCAS;"
        "cccd:CCCD;"
        "phone:SĐT;"
        "unit_code:Mã liên NH;"
        "role:Vai trò;"
        "department_name:Phòng/Đơn vị;"
        "branch_name:Chi nhánh;"
        "agent_code:Mã ĐL;"
        "name:Họ và tên"
    )

    # --- Banca Core (enrich mã chi nhánh / đại lý) ---
    banca_core_enabled: bool = False
    banca_core_url: str = "http://localhost:8996"
    banca_core_kc_base_url: str = "http://localhost:8080"
    banca_core_kc_realm: str = "agribank"
    banca_core_kc_client_id: str = ""
    banca_core_kc_client_secret: str = ""
    banca_core_verify_ssl: bool = True
    banca_core_timeout_seconds: int = 30
    banca_core_token_leeway_seconds: int = 30
    banca_core_match_threshold: float = 0.85
    banca_core_match_suggest_threshold: float = 0.65
    banca_core_match_min_gap: float = 0.08

    # Trường bắt buộc khi tạo user (comma-separated)
    user_required_fields: str = (
        "email,first_name,last_name,branch_code,ipcas_code,cccd,phone,unit_code,role"
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

    @property
    def user_required_fields_list(self) -> list[str]:
        return [f.strip() for f in self.user_required_fields.split(",") if f.strip()]

    @property
    def keycloak_role_map_parsed(self) -> dict[str, str]:
        """{alias_normalized: role_name}"""
        import unicodedata

        def _ascii_alias(s: str) -> str:
            s = s.strip().lower().replace("đ", "d")
            s = unicodedata.normalize("NFD", s)
            return "".join(c for c in s if unicodedata.category(c) != "Mn")

        result: dict[str, str] = {}
        for group in self.keycloak_role_map.split(";"):
            group = group.strip()
            if not group or ":" not in group:
                continue
            alias, role = group.split(":", 1)
            alias = alias.strip().lower()
            role = role.strip()
            if alias and role:
                result[alias] = role
                ascii_alias = _ascii_alias(alias)
                if ascii_alias and ascii_alias not in result:
                    result[ascii_alias] = role
        return result

    @property
    def keycloak_valid_roles(self) -> list[str]:
        return sorted(set(self.keycloak_role_map_parsed.values()))

    @property
    def keycloak_attribute_map_parsed(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for group in self.keycloak_attribute_map.split(";"):
            group = group.strip()
            if not group or ":" not in group:
                continue
            field, attr = group.split(":", 1)
            field, attr = field.strip(), attr.strip()
            if field and attr:
                result[field] = attr
        return result

    @property
    def keycloak_roles_configured(self) -> bool:
        return bool(self.keycloak_roles_client_id.strip())

    @property
    def keycloak_role_assign_configured(self) -> bool:
        cid = self.keycloak_role_assign_client_id.strip()
        if not cid:
            return False
        if cid == self.keycloak_client_id.strip():
            return False
        return bool(self.keycloak_role_assign_client_secret.strip())

    @property
    def field_labels_vi(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for group in self.field_labels_vi_map.split(";"):
            group = group.strip()
            if not group or ":" not in group:
                continue
            field, label = group.split(":", 1)
            field, label = field.strip(), label.strip()
            if field and label:
                result[field] = label
        return result

    @property
    def keycloak_prod_configured(self) -> bool:
        return bool(
            self.keycloak_prod_base_url.strip()
            and self.keycloak_prod_client_id.strip()
            and self.keycloak_prod_client_secret.strip()
        )

    @property
    def banca_core_configured(self) -> bool:
        return bool(
            self.banca_core_enabled
            and self.banca_core_url.strip()
            and self.banca_core_kc_client_id.strip()
            and self.banca_core_kc_client_secret.strip()
        )


settings = Settings()
