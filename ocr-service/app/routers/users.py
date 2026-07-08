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
from app.deps import verify_worker_token
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
from app.services.keycloak_service import (
    REQUIRED_ACTION_CONFIGURE_TOTP,
    REQUIRED_ACTION_UPDATE_PASSWORD,
    KeycloakClient,
    KeycloakConflictError,
    KeycloakError,
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
    client: KeycloakClient, user_id: str, temporary: bool
) -> str:
    for attempt in range(6):
        pwd = _date_based_password("" if attempt == 0 else str(attempt))
        try:
            client.reset_password(user_id, pwd, temporary=temporary)
            return pwd
        except KeycloakError as exc:
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


def _build_kc_client(realm: str) -> KeycloakClient:
    try:
        return KeycloakClient(realm=realm or None)
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc




def _role_assign_client(provision_client: KeycloakClient) -> KeycloakClient:
    """Client dùng để gán role — client riêng nếu cấu hình, không thì tái dùng provision client."""
    if settings.keycloak_role_assign_configured:
        return KeycloakClient(
            realm=provision_client.realm or None,
            client_id=settings.keycloak_role_assign_client_id,
            client_secret=settings.keycloak_role_assign_client_secret,
        )
    return provision_client


def _existing_role_names(
    client: KeycloakClient, user_id: str, client_uuid: str
) -> tuple[set[str], bool]:
    """Trả (tên role đã có, skipped_lookup_403)."""
    existing, err = client.get_user_client_roles_optional(user_id, client_uuid)
    if err == "403":
        logger.warning(
            "Khong doc duoc client roles cua user %s (403) — van thu gan role.",
            user_id,
        )
        return set(), True
    return {str(r.get("name", "")) for r in existing}, False


def _assign_roles_with_client(
    client: KeycloakClient,
    user_id: str,
    client_uuid: str,
    role_names: list[str],
) -> list[str]:
    """Gán role qua một KeycloakClient cụ thể. Trả actions hoặc raise.

    Đồng bộ (reconcile) role: gán role còn thiếu và gỡ những role thuộc bộ
    role hệ thống quản lý nhưng không còn nằm trong danh sách mong muốn.
    """
    applied: list[str] = []
    existing, lookup_skipped = _existing_role_names(client, user_id, client_uuid)
    desired = set(role_names)
    to_assign: list[dict] = []

    for role_name in role_names:
        role_repr = client.get_client_role(client_uuid, role_name)
        if not role_repr:
            raise KeycloakError(
                f"Role '{role_name}' khong ton tai tren client "
                f"'{settings.keycloak_roles_client_id}'."
            )
        if role_name in existing:
            applied.append(f"role_already:{role_name}")
            continue
        to_assign.append(KeycloakClient._role_mapping_payload(role_repr, client_uuid))

    if to_assign:
        client.assign_client_roles_batch(user_id, client_uuid, to_assign)
        for payload in to_assign:
            applied.append(f"assign_role:{payload['name']}")

    # Gỡ role không còn được chọn. Chỉ đụng tới role trong bộ role hệ thống
    # quản lý (keycloak_valid_roles) để không thu hồi role khác của user.
    # Bỏ qua khi không đọc được role hiện có (403) để tránh xóa nhầm.
    if not lookup_skipped:
        managed = set(settings.keycloak_valid_roles)
        to_remove: list[dict] = []
        for name in existing:
            if name not in managed or name in desired:
                continue
            role_repr = client.get_client_role(client_uuid, name)
            if role_repr:
                to_remove.append(
                    KeycloakClient._role_mapping_payload(role_repr, client_uuid)
                )
        if to_remove:
            client.remove_client_roles_batch(user_id, client_uuid, to_remove)
            for payload in to_remove:
                applied.append(f"remove_role:{payload['name']}")

    return applied


def _resolve_roles_client_uuid(client: KeycloakClient) -> str:
    client_id = settings.keycloak_roles_client_id.strip()
    if not client_id:
        raise KeycloakError("KEYCLOAK_ROLES_CLIENT_ID chua cau hinh.")
    if client_id in _client_uuid_cache:
        return _client_uuid_cache[client_id]
    kc_client = client.get_client_by_client_id(client_id)
    if not kc_client or not kc_client.get("id"):
        raise KeycloakError(f"Khong tim thay Keycloak client '{client_id}'.")
    uuid = str(kc_client["id"])
    _client_uuid_cache[client_id] = uuid
    return uuid


def _user_role_names(user: KeycloakUserInput) -> list[str]:
    if user.roles:
        return list(user.roles)
    return [user.role] if user.role else []


def _assign_client_role(
    client: KeycloakClient, user_id: str, role_name: str
) -> list[str]:
    return _assign_client_roles(client, user_id, [role_name] if role_name else [])


def _resolve_role_for_assign(role_name: str) -> str:
    """Resolve role slug for Keycloak assign (passthrough valid slugs)."""
    name = (role_name or "").strip()
    if not name:
        return ""
    if name in settings.keycloak_valid_roles:
        return name
    return normalize_role(name)


def _assign_client_roles(
    client: KeycloakClient, user_id: str, role_names: list[str]
) -> list[str]:
    resolved = []
    for role_name in role_names:
        if not role_name:
            continue
        name = _resolve_role_for_assign(role_name)
        if not name:
            continue
        if name not in settings.keycloak_valid_roles:
            raise KeycloakError(f"Role khong hop le: '{name}'.")
        if name not in resolved:
            resolved.append(name)

    if not resolved:
        return []

    logger.info("Assign roles to user %s: %s", user_id, resolved)
    client_uuid = _resolve_roles_client_uuid(client)
    role_client = _role_assign_client(client)
    clients_to_try = [role_client]
    if role_client is not client:
        clients_to_try.append(client)

    last_403: KeycloakError | None = None
    for kc in clients_to_try:
        try:
            return _assign_roles_with_client(kc, user_id, client_uuid, resolved)
        except KeycloakError as exc:
            if "403" not in str(exc):
                raise
            last_403 = exc
            logger.warning(
                "Gan role user %s that bai 403 voi client '%s': %s",
                user_id,
                kc.client_id,
                exc,
            )

    skipped = [f"assign_role_skipped:{r}" for r in resolved]
    skipped.append("roles_assignment_failed:403")
    if last_403:
        logger.error(
            "Khong gan duoc role cho user %s. Cap realm-management roles "
            "manage-users + manage-clients (hoac realm-admin) cho service account "
            "'%s' hoac cau hinh KEYCLOAK_ROLE_ASSIGN_CLIENT_ID/SECRET. Loi: %s",
            user_id,
            client.client_id,
            last_403,
        )
    return skipped


def _save_existing_user(
    client: KeycloakClient,
    user_id: str,
    user: KeycloakUserInput,
    attrs: dict,
) -> list[str]:
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
    applied.extend(_assign_client_roles(client, user_id, _user_role_names(user)))
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


def _provision_one(
    client: KeycloakClient,
    user: KeycloakUserInput,
    *,
    temporary: bool,
    on_conflict: OnConflictAction,
    default_required_actions: list[str],
) -> UserProvisionResult:
    user = finalize_user(user)
    username = user.username.strip()
    result = UserProvisionResult(username=username, status=ProvisionStatus.FAILED)

    missing = validate_user_fields(user)
    if missing:
        result.error = f"Thieu/khong hop le: {', '.join(missing)}"
        return result

    if not settings.keycloak_roles_configured:
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
            applied.extend(_assign_client_roles(client, user_id, _user_role_names(user)))
            result.status = ProvisionStatus.CREATED
            result.user_id = user_id
            result.actions_applied = applied
            return result

        user_id = str(existing.get("id", ""))
        result.user_id = user_id
        action = user.on_conflict or on_conflict

        applied = _save_existing_user(client, user_id, user, attrs)

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

    except KeycloakConflictError as exc:
        result.error = str(exc)
        return result
    except KeycloakError as exc:
        result.error = str(exc)
        logger.warning("Provision '%s' loi: %s", username, exc)
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




@router.get("/keycloak-role-check", response_model=KeycloakRoleCheckResponse)
async def keycloak_role_check():
    """Kiểm tra service account có đủ quyền gán client role banca-*."""
    if not settings.keycloak_configured:
        raise HTTPException(status_code=503, detail="Keycloak chua cau hinh.")
    if not settings.keycloak_roles_configured:
        raise HTTPException(status_code=503, detail="KEYCLOAK_ROLES_CLIENT_ID chua cau hinh.")

    client = _build_kc_client("")
    resp = KeycloakRoleCheckResponse(
        roles_client_id=settings.keycloak_roles_client_id,
        provision_client_id=settings.keycloak_client_id,
        role_assign_client_id=(
            settings.keycloak_role_assign_client_id
            if settings.keycloak_role_assign_configured
            else settings.keycloak_client_id
        ),
    )
    try:
        client_uuid = _resolve_roles_client_uuid(client)
        resp.can_view_roles_client = True
        sample = settings.keycloak_valid_roles[0] if settings.keycloak_valid_roles else ""
        if sample:
            role_repr = client.get_client_role(client_uuid, sample)
            if role_repr:
                resp.can_assign_test_role = True
                resp.ok = True
                resp.message = f"Co the gan role mau '{sample}'."
    except KeycloakError as exc:
        msg = str(exc)
        resp.message = msg
        if "403" in msg:
            resp.fix_hint = (
                "Vao Keycloak > Clients > "
                f"{settings.keycloak_client_id} > Service account roles > "
                "Assign role > Filter by clients > realm-management > "
                "chon manage-users VA manage-clients (hoac realm-admin). "
                "Hoac cau hinh KEYCLOAK_ROLE_ASSIGN_CLIENT_ID/SECRET voi client co realm-admin."
            )
        elif "404" in msg:
            resp.fix_hint = (
                f"Kiem tra KEYCLOAK_ROLES_CLIENT_ID='{settings.keycloak_roles_client_id}' "
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
async def provision_batch(request: BatchProvisionRequest):
    if not settings.keycloak_configured and not request.realm:
        raise HTTPException(status_code=503, detail="Keycloak chua cau hinh.")

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

    client = _build_kc_client(request.realm)
    response = BatchProvisionResponse(total=len(users))

    for user in users:
        item = _provision_one(
            client,
            user,
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

    return response


@router.get("/preview-from-job/{job_id}", response_model=UserPreviewResponse)
async def preview_from_job(job_id: str):
    from app.services.table_service import get_result

    result = get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Khong tim thay job: {job_id}")
    users, warnings = map_result_to_users(result)
    return UserPreviewResponse(job_id=job_id, total=len(users), users=users, warnings=warnings)
