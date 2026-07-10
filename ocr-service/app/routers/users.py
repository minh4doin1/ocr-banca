"""
Users Router — Tao lo user, enrich, lookup chi nhanh/dai ly.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.deps import get_target_env, get_user_service_client, verify_worker_token
from app.models.schemas import (
    AgencyLookupItem,
    AgencyLookupResponse,
    AgentLookupItem,
    AgentLookupResponse,
    BatchProvisionRequest,
    BatchProvisionResponse,
    EnrichRequest,
    EnrichResponse,
    ErrorResponse,
    FieldConfigResponse,
    KeycloakRoleCheckResponse,
    KeycloakDiagnosticsResponse,
    KeycloakUserInput,
    OnConflictAction,
    ProvisionStatus,
    UserPreviewResponse,
    UserProvisionResult,
    UserValidationItem,
    ValidateUsersRequest,
    ValidateUsersResponse,
)
from app.services.banca_core_service import (
    BancaCoreClient,
    BancaCoreError,
    get_client,
    parse_agency_item,
    parse_agent_enrichment,
)
from app.services.branch_agent_matcher import enrich_user_row
from app.services.keycloak_env import KeycloakProfile, resolve_keycloak_profile
from app.services.keycloak_diagnostics import run_keycloak_diagnostics
from app.services.keycloak_service import (
    REQUIRED_ACTION_CONFIGURE_TOTP,
    REQUIRED_ACTION_UPDATE_PASSWORD,
    KeycloakClient,
    KeycloakConflictError,
    KeycloakError,
)
from app.services.user_service_client import (
    UserConflictError,
    UserServiceAuthError,
    UserServiceClient,
    UserServiceError,
    UserServiceUnavailableError,
)
from app.services.user_mapping import (
    build_keycloak_attributes,
    finalize_user,
    get_sso_column_labels,
    map_result_to_users,
    normalize_role,
    validate_user_field_errors,
    validate_user_fields,
)

logger = logging.getLogger(__name__)

_ROLE_LABELS: dict[str, str] = {
    "banca-admin": "Quan tri",
    "banca-seller": "Dai ly vien",
    "banca-accounting-operator": "Ke toan vien",
    "banca-accounting-controller": "Phe duyet vien",
}

_client_uuid_cache: dict[str, str] = {}

router = APIRouter(
    prefix="/api/users",
    tags=["Users"],
    dependencies=[Depends(verify_worker_token)],
)


def _generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*" for c in pwd)
        ):
            return pwd


def _date_based_password(suffix: str = "") -> str:
    base = f"Ngay{datetime.now():%d%m%Y}@"
    return f"{base}{suffix}" if suffix else base


def _reset_password_with_retry(
    client: UserServiceClient, user_id: str, temporary: bool
) -> str:
    for attempt in range(6):
        pwd = _date_based_password("" if attempt == 0 else str(attempt))
        try:
            client.reset_password(user_id, pwd, temporary=temporary)
            return pwd
        except UserServiceError as exc:
            msg = str(exc).lower()
            if attempt < 5 and ("password" in msg or "history" in msg or "400" in msg):
                continue
            raise
    raise KeycloakError("Khong the dat mat khau moi sau nhieu lan thu.")


def _resolve_temp_password(user: KeycloakUserInput) -> str:
    if user.password:
        return user.password
    if settings.keycloak_default_temp_password:
        return settings.keycloak_default_temp_password
    return _generate_temp_password()


def _build_kc_client(realm: str, kc: KeycloakProfile) -> KeycloakClient:
    if not kc.configured:
        raise HTTPException(
            status_code=503,
            detail=f"Keycloak {kc.env.upper()} chua cau hinh.",
        )
    try:
        return KeycloakClient(
            base_url=kc.base_url,
            realm=realm or kc.realm,
            client_id=kc.client_id,
            client_secret=kc.client_secret,
            verify_ssl=kc.verify_ssl,
        )
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _role_assign_client(
    provision_client: KeycloakClient, kc: KeycloakProfile
) -> KeycloakClient:
    """Client dùng để gán role — client riêng nếu cấu hình, không thì tái dùng provision client."""
    if kc.role_assign_configured:
        return KeycloakClient(
            base_url=kc.base_url,
            realm=provision_client.realm or kc.realm,
            client_id=kc.role_assign_client_id,
            client_secret=kc.role_assign_client_secret,
            verify_ssl=kc.verify_ssl,
        )
    return provision_client


def _resolve_roles_client_uuid(
    client: KeycloakClient, kc: KeycloakProfile
) -> str:
    client_id = kc.roles_client_id.strip()
    if not client_id:
        raise KeycloakError("KEYCLOAK_ROLES_CLIENT_ID chua cau hinh.")
    cache_key = f"{kc.base_url}:{client_id}"
    if cache_key in _client_uuid_cache:
        return _client_uuid_cache[cache_key]
    # Bypass GET /clients khi WAF chặn (điền KEYCLOAK_PROD_ROLES_CLIENT_UUID)
    if kc.roles_client_uuid.strip():
        uuid = kc.roles_client_uuid.strip()
        _client_uuid_cache[cache_key] = uuid
        return uuid
    kc_client = client.get_client_by_client_id(client_id)
    if not kc_client or not kc_client.get("id"):
        raise KeycloakError(f"Khong tim thay Keycloak client '{client_id}'.")
    uuid = str(kc_client["id"])
    _client_uuid_cache[cache_key] = uuid
    return uuid


def _user_role_names(user: KeycloakUserInput) -> list[str]:
    if user.roles:
        return list(user.roles)
    return [user.role] if user.role else []


def _resolve_role_for_assign(role_name: str) -> str:
    """Resolve role slug (passthrough valid slugs)."""
    name = (role_name or "").strip()
    if not name:
        return ""
    if name in settings.keycloak_valid_roles:
        return name
    return normalize_role(name)


# ── Role assignment qua user-service ──


def _assign_roles_via_user_service(
    client: UserServiceClient,
    user_id: str,
    role_names: list[str],
) -> list[str]:
    """
    Gán + gỡ role (reconcile) qua user-service.

    Quy tắc:
      - Gán role còn thiếu so với `role_names`
      - Gỡ role thuộc bộ role hệ thống quản lý (`keycloak_valid_roles`)
        nhưng không còn trong `role_names`
      - Trả về danh sách action đã thực hiện (string)
    """
    resolved: list[str] = []
    for role_name in role_names:
        name = _resolve_role_for_assign(role_name)
        if not name:
            continue
        if name not in settings.keycloak_valid_roles:
            raise KeycloakError(f"Role khong hop le: '{name}'.")
        if name not in resolved:
            resolved.append(name)

    if not resolved:
        return []

    logger.info("Assign roles to user %s via user-service: %s", user_id, resolved)

    # Lấy role hiện có
    existing_roles = client.get_user_client_roles(user_id)
    existing_names = {r.get("name", "") for r in existing_roles if r.get("name")}
    desired = set(resolved)
    managed = set(settings.keycloak_valid_roles)

    applied: list[str] = []

    # Gán role còn thiếu
    to_assign = [n for n in resolved if n not in existing_names]
    if to_assign:
        result = client.assign_roles(user_id, to_assign)
        for n in result.get("assigned", []):
            applied.append(f"assign_role:{n}")
        for n in result.get("skipped", []):
            applied.append(f"role_already:{n}")

    # Gỡ role managed không còn được chọn
    to_remove = [
        n for n in existing_names if n in managed and n not in desired
    ]
    if to_remove:
        result = client.remove_roles(user_id, to_remove)
        for n in result.get("removed", []):
            applied.append(f"remove_role:{n}")
        for n in result.get("skipped", []):
            applied.append(f"role_skip:{n}")

    return applied


def _save_existing_user_via_user_service(
    client: UserServiceClient,
    user_id: str,
    user: KeycloakUserInput,
    attrs: dict,
) -> list[str]:
    """Cập nhật user hiện có qua user-service."""
    applied: list[str] = []
    display_name = user.name.strip()
    first = user.first_name.strip() or (
        display_name.split()[-1] if display_name else ""
    )
    last = user.last_name.strip() or (
        " ".join(display_name.split()[:-1]) if display_name else ""
    )
    client.update_user_details(
        user_id,
        email=user.email or user.username,
        first_name=first,
        last_name=last,
    )
    applied.append("save_details")
    if attrs:
        client.update_user_attributes(user_id, attrs)
        applied.append("set_attributes")
    applied.extend(
        _assign_roles_via_user_service(client, user_id, _user_role_names(user))
    )
    return applied


def _require_banca() -> BancaCoreClient:
    client = get_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Banca Core chua bat hoac thieu cau hinh KC client.",
        )
    return client


def _resolve_users_list(
    job_id: str, users: list[KeycloakUserInput]
) -> tuple[list[KeycloakUserInput], list[str]]:
    if job_id:
        from app.services.table_service import get_result

        result = get_result(job_id)
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"Khong tim thay job OCR: {job_id}"
            )
        return map_result_to_users(result)
    return list(users), []


def _enrich_users(
    users: list[KeycloakUserInput],
    defaults: dict[str, str] | None = None,
) -> EnrichResponse:
    client = get_client()
    warnings: list[str] = []
    enriched: list[KeycloakUserInput] = []
    defaults = defaults or {}

    for user in users:
        row = user.model_dump()
        for key, val in defaults.items():
            if val and not str(row.get(key) or "").strip():
                row[key] = val
        row = enrich_user_row(row, client)
        updated = KeycloakUserInput.model_validate(row)
        updated = finalize_user(updated)
        updated.missing_fields = validate_user_fields(updated)
        if updated.warnings:
            warnings.extend(updated.warnings)
        enriched.append(updated)

    return EnrichResponse(users=enriched, warnings=warnings)


def _provision_one_via_user_service(
    client: UserServiceClient,
    user: KeycloakUserInput,
    *,
    kc: KeycloakProfile,
    temporary: bool,
    on_conflict: OnConflictAction,
    default_required_actions: list[str],
) -> UserProvisionResult:
    """
    Provision 1 user qua user-service (Node.js BE).

    Không cần:
      - resolve UUID (user-service lo)
      - retry với client khác trên 403 (user-service dùng 1 client)
      - lookup client/role (user-service lo)
    """
    user = finalize_user(user)
    username = user.username.strip()
    result = UserProvisionResult(username=username, status=ProvisionStatus.FAILED)

    missing = validate_user_fields(user)
    if missing:
        result.error = f"Thieu/khong hop le: {', '.join(missing)}"
        return result

    if not kc.roles_configured:
        result.error = "KEYCLOAK_ROLES_CLIENT_ID chua cau hinh."
        return result

    attrs = build_keycloak_attributes(user)
    display_name = user.name.strip()
    first = user.first_name.strip() or (
        display_name.split()[-1] if display_name else ""
    )
    last = user.last_name.strip() or (
        " ".join(display_name.split()[:-1]) if display_name else ""
    )

    try:
        existing = client.find_user_by_username(username)

        if existing is None:
            required_actions = (
                user.required_actions
                if user.required_actions is not None
                else default_required_actions
            )
            user_id = client.create_user(
                username=username,
                email=user.email or username,
                first_name=first,
                last_name=last,
                password=_resolve_temp_password(user),
                temporary=temporary,
                required_actions=required_actions,
                attributes=attrs,
            )
            applied = ["create"] + list(required_actions)
            if attrs:
                applied.append("set_attributes")
            applied.extend(
                _assign_roles_via_user_service(client, user_id, _user_role_names(user))
            )
            result.status = ProvisionStatus.CREATED
            result.user_id = user_id
            result.actions_applied = applied
            return result

        user_id = str(existing.get("id", ""))
        result.user_id = user_id
        action = user.on_conflict or on_conflict

        applied = _save_existing_user_via_user_service(
            client, user_id, user, attrs
        )

        reset_password = action in (
            OnConflictAction.RESET_PASSWORD,
            OnConflictAction.RESET_BOTH,
        )
        reset_otp = action in (
            OnConflictAction.RESET_OTP,
            OnConflictAction.RESET_BOTH,
        )

        if reset_password:
            # Ép người dùng đổi mật khẩu ngay lần đăng nhập kế tiếp.
            _reset_password_with_retry(client, user_id, temporary=True)
            applied.append("reset_password")

        # Reset OTP dùng set_required_actions (ghi đè) nên phải chạy TRƯỚC,
        # sau đó merge UPDATE_PASSWORD để không xóa mất yêu cầu đổi mật khẩu.
        if reset_otp:
            deleted = client.reset_otp(user_id)
            applied.append(f"reset_otp(deleted={deleted})")

        if reset_password:
            client.ensure_required_actions(
                user_id, [REQUIRED_ACTION_UPDATE_PASSWORD]
            )
            applied.append(f"require_action:{REQUIRED_ACTION_UPDATE_PASSWORD}")

        result.status = ProvisionStatus.UPDATED
        result.actions_applied = applied
        return result

    except UserConflictError as exc:
        result.error = str(exc)
        return result
    except (UserServiceAuthError, UserServiceUnavailableError) as exc:
        # user-service không reach được hoặc auth lỗi — surface rõ cho caller
        result.error = f"user-service unavailable: {exc}"
        logger.error(
            "Provision '%s' env=%s fail (user-service): %s",
            username,
            kc.env,
            exc,
        )
        return result
    except UserServiceError as exc:
        result.error = str(exc)
        logger.warning(
            "Provision '%s' env=%s loi: %s",
            username,
            kc.env,
            exc,
            exc_info=settings.keycloak_debug,
        )
        return result


@router.get("/lookup/agencies", response_model=AgencyLookupResponse)
async def lookup_agencies(
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    client = _require_banca()
    try:
        raw = client.search_agencies(search, page=page, size=size)
        items = [AgencyLookupItem(**parse_agency_item(a)) for a in raw]
        return AgencyLookupResponse(items=items, total=len(items))
    except BancaCoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/lookup/agents", response_model=AgentLookupResponse)
async def lookup_agents(
    search: str = Query(default=""),
    agency_id: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    client = _require_banca()
    try:
        raw = client.search_agents(search, agency_id=agency_id, page=page, size=size)
        items: list[AgentLookupItem] = []
        for a in raw:
            parsed = parse_agent_enrichment(a)
            items.append(
                AgentLookupItem(
                    id=a.get("id", ""),
                    name=a.get("name", ""),
                    email=a.get("email", ""),
                    ipcas_code=a.get("ipcasCode", ""),
                    branch_code=parsed["branch_code"],
                    agent_code=parsed["agent_code"],
                )
            )
        return AgentLookupResponse(items=items, total=len(items))
    except BancaCoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/field-config", response_model=FieldConfigResponse)
async def field_config():
    roles = [
        {"value": role, "label": _ROLE_LABELS.get(role, role)}
        for role in settings.keycloak_valid_roles
    ]
    return FieldConfigResponse(
        required_fields=settings.user_required_fields_list,
        header_map=settings.keycloak_header_map_parsed,
        field_labels=settings.field_labels_vi,
        sso_columns=get_sso_column_labels(),
        banca_core_enabled=settings.banca_core_configured,
        roles=roles,
        attribute_keys=settings.keycloak_attribute_map_parsed,
        roles_client_id=settings.keycloak_roles_client_id,
        default_temp_password=settings.keycloak_default_temp_password,
    )


@router.post("/validate", response_model=ValidateUsersResponse)
async def validate_users(request: ValidateUsersRequest):
    items: list[UserValidationItem] = []
    valid = 0
    for idx, user in enumerate(request.users):
        errors = validate_user_field_errors(user)
        missing = list(errors.keys())
        if not missing:
            valid += 1
        items.append(
            UserValidationItem(
                index=idx,
                username=user.username or user.email,
                missing_fields=missing,
                field_errors=errors,
            )
        )
    return ValidateUsersResponse(
        users=items,
        valid_count=valid,
        invalid_count=len(request.users) - valid,
    )


@router.post("/enrich", response_model=EnrichResponse)
async def enrich_users(request: EnrichRequest):
    users, warnings = _resolve_users_list(request.job_id, request.users)
    if not users:
        detail = "Khong co user de enrich."
        if warnings:
            detail += " " + " ".join(warnings)
        raise HTTPException(status_code=400, detail=detail)
    resp = _enrich_users(users, defaults=request.defaults or None)
    resp.warnings = warnings + resp.warnings
    return resp




@router.get("/keycloak-diagnostics", response_model=KeycloakDiagnosticsResponse)
async def keycloak_diagnostics(target_env: str = Depends(get_target_env)):
    """Chẩn đoán Keycloak từng bước (DNS, token, users, clients, role)."""
    return run_keycloak_diagnostics(target_env)


@router.get("/keycloak-role-check", response_model=KeycloakRoleCheckResponse)
async def keycloak_role_check(target_env: str = Depends(get_target_env)):
    """Kiểm tra service account có đủ quyền gán client role banca-*."""
    kc = resolve_keycloak_profile(target_env)
    if not kc.configured:
        raise HTTPException(
            status_code=503,
            detail=f"Keycloak {kc.env.upper()} chua cau hinh.",
        )
    if not kc.roles_configured:
        raise HTTPException(status_code=503, detail="KEYCLOAK_ROLES_CLIENT_ID chua cau hinh.")

    client = _build_kc_client("", kc)
    resp = KeycloakRoleCheckResponse(
        roles_client_id=kc.roles_client_id,
        provision_client_id=kc.client_id,
        role_assign_client_id=(
            kc.role_assign_client_id if kc.role_assign_configured else kc.client_id
        ),
    )
    try:
        client_uuid = _resolve_roles_client_uuid(client, kc)
        resp.can_view_roles_client = True
        sample = settings.keycloak_valid_roles[0] if settings.keycloak_valid_roles else ""
        if sample:
            role_repr = client.get_client_role(client_uuid, sample)
            if role_repr:
                resp.can_assign_test_role = True
                resp.ok = True
                resp.message = f"Co the gan role mau '{sample}' ({kc.env.upper()})."
    except KeycloakError as exc:
        msg = str(exc)
        resp.message = msg
        if "403" in msg:
            resp.fix_hint = (
                "Vao Keycloak > Clients > "
                f"{kc.client_id} > Service account roles > "
                "Assign role > Filter by clients > realm-management > "
                "chon manage-users VA manage-clients (hoac realm-admin). "
                "Hoac cau hinh KEYCLOAK_ROLE_ASSIGN_CLIENT_ID/SECRET voi client co realm-admin."
            )
        elif "404" in msg:
            resp.fix_hint = (
                f"Kiem tra KEYCLOAK_ROLES_CLIENT_ID='{kc.roles_client_id}' "
                "va KEYCLOAK_REALM."
            )
        else:
            resp.fix_hint = "Kiem tra KEYCLOAK_BASE_URL, client secret va realm."
    return resp

@router.post(
    "/provision-batch",
    response_model=BatchProvisionResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def provision_batch(
    request: BatchProvisionRequest,
    target_env: str = Depends(get_target_env),
):
    kc = resolve_keycloak_profile(target_env)

    # Resolve user-service qua factory (raise 503 nếu chưa cấu hình)
    client = get_user_service_client()

    if not kc.roles_configured:
        raise HTTPException(
            status_code=503,
            detail="KEYCLOAK_ROLES_CLIENT_ID chua cau hinh (can de user-service biet client nao chua role banca-*).",
        )

    users, warnings = _resolve_users_list(request.job_id, request.users)
    if not users:
        detail = "Khong co user nao de xu ly."
        if warnings:
            detail += " " + " ".join(warnings)
        raise HTTPException(status_code=400, detail=detail)

    if request.job_id or not request.users:
        users = _enrich_users(users).users

    temporary = (
        settings.keycloak_default_temporary
        if request.default_temporary is None
        else request.default_temporary
    )
    default_required_actions = (
        request.default_required_actions
        if request.default_required_actions is not None
        else settings.keycloak_default_required_actions_list
    )

    response = BatchProvisionResponse(total=len(users))
    logger.info(
        "provision_batch via user-service=%s env=%s roles_client=%s users=%d",
        settings.user_service_url,
        kc.env,
        settings.user_service_roles_client_id,
        len(users),
    )

    for user in users:
        item = _provision_one_via_user_service(
            client,
            user,
            kc=kc,
            temporary=temporary,
            on_conflict=request.default_on_conflict,
            default_required_actions=default_required_actions,
        )
        response.results.append(item)
        if item.status == ProvisionStatus.CREATED:
            response.created += 1
        elif item.status == ProvisionStatus.UPDATED:
            response.updated += 1
        elif item.status == ProvisionStatus.SKIPPED:
            response.skipped += 1
        else:
            response.failed += 1
        logger.info(
            "provision result user=%s status=%s error=%s actions=%s",
            item.username,
            item.status,
            item.error or "",
            item.actions_applied,
        )

    return response


@router.get("/preview-from-job/{job_id}", response_model=UserPreviewResponse)
async def preview_from_job(job_id: str):
    from app.services.table_service import get_result

    result = get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Khong tim thay job: {job_id}")
    users, warnings = map_result_to_users(result)
    return UserPreviewResponse(job_id=job_id, total=len(users), users=users, warnings=warnings)
