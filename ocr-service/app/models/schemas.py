"""Pydantic schemas for OCR Service API."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class JobStatus(str, enum.Enum):
    """OCR job processing status."""

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PageStatus(str, enum.Enum):
    """Per-page OCR processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class LogLevel(str, enum.Enum):
    """Job log entry severity."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"


class JobLogEntry(BaseModel):
    """Single log line streamed to the client during processing."""

    timestamp: datetime = Field(default_factory=datetime.now)
    level: LogLevel = LogLevel.INFO
    message: str


class PageStatusInfo(BaseModel):
    """Status of a single PDF page within a job."""

    page_number: int
    status: PageStatus = PageStatus.PENDING
    error_message: str = ""


class ProcessingMode(str, enum.Enum):
    """OCR processing mode chosen at upload time."""

    LOCAL = "local"
    REMOTE = "remote"
    API = "api"
    AUTO = "auto"


class RemoteProvider(str, enum.Enum):
    """Remote OCR worker provider."""

    INTERNAL = "internal"
    COLAB = "colab"
    CUSTOM = "custom"


class OcrEnvProfile(BaseModel):
    """Frontend API target for dev/prod switching."""

    id: str = Field(..., description="dev | prod")
    label: str = ""
    api_url: str = Field(..., description="Base URL OCR API (empty = same origin)")
    keycloak_configured: bool = Field(
        default=False,
        description="True khi profile Keycloak (KEYCLOAK_PROD_*) đã cấu hình",
    )
    keycloak_label: str = Field(
        default="",
        description="Nhãn hiển thị (vd URL Keycloak prod, không có secret)",
    )


class OcrEnvironmentsResponse(BaseModel):
    """Available OCR API environments for the frontend switcher."""

    server_env: str = Field(
        default="dev",
        description="APP_ENV của instance backend trả response",
    )
    profiles: list[OcrEnvProfile] = Field(default_factory=list)


class OcrRuntimeConfig(BaseModel):
    """Runtime options exposed to the frontend."""

    internal_gpu_configured: bool = False
    internal_gpu_label: str = ""
    worker_token_required: bool = False
    local_gpu_available: bool = False
    local_gpu_name: str = ""
    local_gpu_detail: str = ""
    paddle_use_gpu: bool = False
    processing_modes: list[str] = Field(
        default_factory=lambda: ["local", "remote", "auto", "api"]
    )
    remote_providers: list[str] = Field(
        default_factory=lambda: ["internal", "colab", "custom"]
    )


class CellData(BaseModel):
    """Single cell in a table."""

    row: int = Field(..., description="Row index (0-based)")
    col: int = Field(..., description="Column index (0-based)")
    text: str = Field(..., description="OCR recognized text")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="OCR confidence score"
    )
    bbox: list[int] = Field(
        default_factory=list,
        description="Bounding box [x1, y1, x2, y2] on original image",
    )


class TableData(BaseModel):
    """Extracted table data from one region."""

    table_index: int = Field(..., description="Table index on page")
    num_rows: int = Field(..., description="Number of rows")
    num_cols: int = Field(..., description="Number of columns")
    cells: list[CellData] = Field(default_factory=list)
    html: str = Field(default="", description="Table as HTML (from PP-Structure)")
    table_kind: str = Field(
        default="",
        description="sso_agribank when Agribank SSO 10-column form detected",
    )


class PageResult(BaseModel):
    """OCR result for a single PDF page."""

    page_number: int = Field(..., description="Page number (1-based)")
    image_path: str = Field(default="", description="Path to page image")
    tables: list[TableData] = Field(default_factory=list)
    raw_text: str = Field(
        default="", description="Full page text (non-table regions)"
    )


class OcrResult(BaseModel):
    """Complete OCR result for an uploaded PDF."""

    job_id: str
    filename: str
    total_pages: int = 0
    pages: list[PageResult] = Field(default_factory=list)
    is_complete: bool = True
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class JobInfo(BaseModel):
    """Job status information returned to client."""

    job_id: str
    filename: str
    processing_mode: ProcessingMode = ProcessingMode.LOCAL
    api_provider: str = ""
    use_gpu: bool = False
    remote_provider: RemoteProvider | None = None
    remote_url: str = ""
    remote_job_id: str = ""
    status: JobStatus
    total_pages: int = 0
    progress: int = Field(0, description="Number of pages processed")
    page_statuses: list[PageStatusInfo] = Field(default_factory=list)
    logs: list[JobLogEntry] = Field(default_factory=list)
    error_message: str = ""
    queue_position: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class UploadResponse(BaseModel):
    """Response after uploading a PDF."""

    job_id: str
    filename: str
    processing_mode: ProcessingMode = ProcessingMode.LOCAL
    api_provider: str = ""
    use_gpu: bool = False
    remote_provider: RemoteProvider | None = None
    remote_url: str = ""
    queue_position: int = 0
    message: str = "PDF uploaded successfully. Processing started."


class RemoteWorkerHealth(BaseModel):
    """Health check result for a remote OCR worker."""

    url: str
    reachable: bool
    status: str = ""
    detail: str = ""
    use_gpu: bool | None = None


class UpdateCellRequest(BaseModel):
    """Request to update a single cell value after review."""

    page_number: int
    table_index: int
    row: int
    col: int
    text: str


class UpdateResultRequest(BaseModel):
    """Request to update OCR result after user review."""

    updates: list[UpdateCellRequest]


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str


# ──────────────────────────────────────────────────────────────
# Keycloak user provisioning
# ──────────────────────────────────────────────────────────────


class OnConflictAction(str, enum.Enum):
    """Hành động khi user đã tồn tại trong Keycloak."""

    SKIP = "skip"
    RESET_PASSWORD = "reset_password"
    RESET_OTP = "reset_otp"
    RESET_BOTH = "reset_both"


class MatchStatus(str, enum.Enum):
    """Kết quả auto-match chi nhánh/đại lý."""

    AUTO = "auto"
    SUGGEST = "suggest"
    MANUAL = "manual"


class ProvisionStatus(str, enum.Enum):
    """Kết quả xử lý một user trong lô."""

    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


class KeycloakUserInput(BaseModel):
    """Một user đầu vào cho việc tạo lô."""

    username: str = Field(..., description="Tên đăng nhập (bắt buộc)")
    email: str = Field(default="", description="Email")
    name: str = Field(default="", description="Họ tên đầy đủ")
    first_name: str = Field(default="", description="Tên")
    last_name: str = Field(default="", description="Họ")
    cccd: str = Field(default="", description="Số CCCD/CMND")
    branch_name: str = Field(default="", description="Tên chi nhánh (OCR/manual)")
    department_name: str = Field(default="", description="Tên phòng GD/PGD")
    branch_code: str = Field(default="", description="Mã chi nhánh")
    agent_code: str = Field(default="", description="Mã đại lý")
    ipcas_code: str = Field(default="", description="Mã IPCAS")
    phone: str = Field(default="", description="Số điện thoại")
    unit_code: str = Field(default="", description="Mã đơn vị")
    notes: str = Field(default="", description="Ghi chú (form SSO, không bắt buộc)")
    role: str = Field(default="", description="Client role Keycloak (banca-*) — role đầu tiên")
    role_raw: str = Field(default="", description="Văn bản role thô từ OCR (trước khi map)")
    roles: list[str] = Field(
        default_factory=list,
        description="Danh sách client roles (hỗ trợ nhiều role/user).",
    )
    branch_name_matched: str = Field(default="", description="Tên CN khớp từ banca-core")
    department_name_matched: str = Field(default="", description="Tên PGD khớp")
    match_status: MatchStatus | None = Field(default=None)
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    enrich_source: str = Field(default="", description="auto|email|manual")
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    password: str = Field(
        default="",
        description="Mật khẩu tạm. Bỏ trống để dùng mặc định/sinh ngẫu nhiên.",
    )
    on_conflict: OnConflictAction | None = Field(
        default=None,
        description="Hành động khi user đã tồn tại (ghi đè mặc định của lô).",
    )
    required_actions: list[str] | None = Field(
        default=None,
        description="Ghi đè required actions cho user này (nếu cần).",
    )
    attributes: dict[str, list[str]] | None = Field(
        default=None,
        description="Keycloak user attributes ghi đè.",
    )


class BatchProvisionRequest(BaseModel):
    """Yêu cầu tạo lô user. Cung cấp job_id HOẶC users."""

    job_id: str = Field(
        default="",
        description="Lấy dữ liệu từ kết quả OCR đã review theo job_id.",
    )
    users: list[KeycloakUserInput] = Field(
        default_factory=list,
        description="Danh sách user trực tiếp (nếu không dùng job_id).",
    )
    realm: str = Field(
        default="",
        description="Ghi đè realm Keycloak (mặc định lấy từ cấu hình).",
    )
    default_temporary: bool | None = Field(
        default=None,
        description="Mật khẩu tạm (temporary). Mặc định theo cấu hình.",
    )
    default_on_conflict: OnConflictAction = Field(
        default=OnConflictAction.SKIP,
        description="Hành động mặc định khi user đã tồn tại.",
    )
    default_required_actions: list[str] | None = Field(
        default=None,
        description="Required actions mặc định khi tạo user. Mặc định theo cấu hình.",
    )


class UserProvisionResult(BaseModel):
    """Kết quả xử lý cho một user."""

    username: str
    status: ProvisionStatus
    user_id: str = ""
    actions_applied: list[str] = Field(default_factory=list)
    error: str = ""


class BatchProvisionResponse(BaseModel):
    """Tổng hợp kết quả tạo lô user."""

    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[UserProvisionResult] = Field(default_factory=list)


class UserPreviewResponse(BaseModel):
    """Danh sách user được map từ job OCR để review trước khi tạo."""

    job_id: str
    total: int = 0
    users: list[KeycloakUserInput] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EnrichRequest(BaseModel):
    """Yêu cầu enrich mã chi nhánh/đại lý."""

    job_id: str = ""
    users: list[KeycloakUserInput] = Field(default_factory=list)
    defaults: dict[str, str] = Field(
        default_factory=dict,
        description="Giá trị mặc định áp dụng mọi dòng trước enrich (branch_code, unit_code, role).",
    )


class BranchAgentMatchResult(BaseModel):
    """Kết quả auto-match một dòng."""

    branch_code: str = ""
    agent_code: str = ""
    agency_id: str = ""
    branch_name_matched: str = ""
    department_name_matched: str = ""
    match_status: MatchStatus = MatchStatus.MANUAL
    match_confidence: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class AgencyLookupItem(BaseModel):
    id: str = ""
    name: str = ""
    core_bank_code: str = ""
    agency_code: str = ""
    status: str = ""


class AgentLookupItem(BaseModel):
    id: str = ""
    name: str = ""
    email: str = ""
    ipcas_code: str = ""
    branch_code: str = ""
    agent_code: str = ""


class AgencyLookupResponse(BaseModel):
    items: list[AgencyLookupItem] = Field(default_factory=list)
    total: int = 0


class AgentLookupResponse(BaseModel):
    items: list[AgentLookupItem] = Field(default_factory=list)
    total: int = 0


class EnrichResponse(BaseModel):
    users: list[KeycloakUserInput] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FieldConfigResponse(BaseModel):
    required_fields: list[str] = Field(default_factory=list)
    header_map: dict[str, list[str]] = Field(default_factory=dict)
    field_labels: dict[str, str] = Field(default_factory=dict)
    sso_columns: list[dict[str, str]] = Field(default_factory=list)
    banca_core_enabled: bool = False
    roles: list[dict[str, str]] = Field(default_factory=list)
    attribute_keys: dict[str, str] = Field(default_factory=dict)
    roles_client_id: str = ""
    default_temp_password: str = ""


class KeycloakRoleCheckResponse(BaseModel):
    ok: bool = False
    roles_client_id: str = ""
    provision_client_id: str = ""
    role_assign_client_id: str = ""
    can_view_roles_client: bool = False
    can_assign_test_role: bool = False
    message: str = ""
    fix_hint: str = ""


class KeycloakDiagStep(BaseModel):
    """Một bước trong battery test Keycloak."""

    step: str = ""
    ok: bool = False
    message: str = ""
    detail: str = ""
    status_code: int | None = None
    content_type: str = ""


class KeycloakDiagnosticsResponse(BaseModel):
    """Kết quả chẩn đoán Keycloak (gọi GET /api/users/keycloak-diagnostics)."""

    ok: bool = False
    target_env: str = "dev"
    base_url: str = ""
    realm: str = ""
    provision_client_id: str = ""
    roles_client_id: str = ""
    roles_client_uuid_configured: bool = False
    verify_ssl: bool = True
    summary: str = ""
    steps: list[KeycloakDiagStep] = Field(default_factory=list)
    log_hint: str = (
        "Xem chi tiết request: ocr-service/logs/keycloak.log "
        "(bật KEYCLOAK_DEBUG=true trong .env)"
    )


class UserValidationItem(BaseModel):
    index: int = 0
    username: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    field_errors: dict[str, str] = Field(default_factory=dict)


class ValidateUsersRequest(BaseModel):
    users: list[KeycloakUserInput] = Field(default_factory=list)


class ValidateUsersResponse(BaseModel):
    users: list[UserValidationItem] = Field(default_factory=list)
    valid_count: int = 0
    invalid_count: int = 0


class OcrCellValidationIssue(BaseModel):
    page_number: int = 0
    table_index: int = 0
    row: int = 0
    col: int = 0
    field: str = ""
    message: str = ""
    severity: str = "error"


class OcrValidationResponse(BaseModel):
    errors: list[OcrCellValidationIssue] = Field(default_factory=list)
    warnings: list[OcrCellValidationIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
