"""
Users Router — Tạo lô user, enrich, lookup chi nhánh/đại lý.
"""

from __future__ import annotations

import logging
import secrets
import string

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
    "banca-admin": "Quản trị",
    "banca-seller": "Đại lý viên",
    "banca-accounting-operator": "Kế toán viên",
    "banca-accounting-controller": "Phê duyệt viên",
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


def _resolve_roles_client_uuid(client: KeycloakClient) -> str:
    client_id = settings.keycloak_roles_client_id.strip()
    if not client_id:
        raise KeycloakError("KEYCLOAK_ROLES_CLIENT_ID chưa cấu hình.")
    if client_id in _client_uuid_cache:
        return _client_uuid_cache[client_id]
    kc_client = client.get_client_by_client_id(client_id)
    if not kc_client or not kc_client.get("id"):
        raise KeycloakError(f"Không tìm thấy Keycloak client '{client_id}'.")
    uuid = str(kc_client["id"])
    _client_uuid_cache[client_id] = uuid
    return uuid


def _assign_client_role(
    client: KeycloakClient, user_id: str, role_name: str
) -> list[str]:
    """Gán client role nếu user chưa có. Trả actions applied."""
    if not role_name:
        return []
    role_name = normalize_role(role_name)
    if role_name not in settings.keycloak_valid_roles:
        raise KeycloakError(f"Role không hợp lệ: '{role_name}'.")

    try:
        client_uuid = _resolve_roles_client_uuid(client)
        role_repr = client.get_client_role(client_uuid, role_name)
        if not role_repr:
            raise KeycloakError(
                f"Role '{role_name}' không tồn tại trên client "
                f"'{settings.keycloak_roles_client_id}'."
            )

        existing = client.get_user_client_roles(user_id, client_uuid)
        if any(str(r.get("name", "")) == role_name for r in existing):
            return [f"role_already:{role_name}"]

        client.assign_client_roles(user_id, client_uuid, [role_repr])
        return [f"assign_role:{role_name}"]
    except KeycloakError as exc:
        # Some realms deny role-client lookup for service account; do not block
        # user creation/update if profile attributes were already saved.
        if "403" in str(exc):
            logger.warning("Skip role assignment for '%s': %s", user_id, exc)
            return [f"assign_role_skipped:{role_name}"]
        raise


def _save_existing_user(
    client: KeycloakClient,
    user_id: str,
    user: KeycloakUserInput,
    attrs: dict,
) -> list[str]:
    """Save Details + attributes theo SOP manual."""
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
    applied.extend(_assign_client_role(client, user_id, user.role))
    return applied


def _require_banca() -> BancaCoreClient:
    client = get_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Banca Core chưa bật hoặc thiếu cấu hình KC client.",
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
                status_code=404, detail=f"Không tìm thấy job OCR: {job_id}"
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
        result.error = f"Thiếu/không hợp lệ: {', '.join(missing)}"
        return result

    if not settings.keycloak_roles_configured:
        result.error = "KEYCLOAK_ROLES_CLIENT_ID chưa cấu hình."
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
            applied.extend(_assign_client_role(client, user_id, user.role))
            result.status = ProvisionStatus.CREATED
            result.user_id = user_id
            result.actions_applied = applied
            return result

        user_id = str(existing.get("id", ""))
        result.user_id = user_id
        action = user.on_conflict or on_conflict

        applied = _save_existing_user(client, user_id, user, attrs)

        if action in (OnConflictAction.RESET_PASSWORD, OnConflictAction.RESET_BOTH):
            client.reset_password(
                user_id, _resolve_temp_password(user), temporary=temporary
            )
            client.ensure_required_actions(
                user_id, [REQUIRED_ACTION_UPDATE_PASSWORD]
            )
            applied.append("reset_password")

        if action in (OnConflictAction.RESET_OTP, OnConflictAction.RESET_BOTH):
            deleted = client.reset_otp(user_id)
            applied.append(f"reset_otp(deleted={deleted})")

        result.status = ProvisionStatus.UPDATED
        result.actions_applied = applied
        return result

    except KeycloakConflictError as exc:
        result.error = str(exc)
        return result
    except KeycloakError as exc:
        result.error = str(exc)
        logger.warning("Provision '%s' lỗi: %s", username, exc)
        return result


# ── Lookup (proxy banca-core) ──


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
        {
            "value": role,
            "label": _ROLE_LABELS.get(role, role),
        }
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
        detail = "Không có user để enrich."
        if warnings:
            detail += " " + " ".join(warnings)
        raise HTTPException(status_code=400, detail=detail)
    resp = _enrich_users(users, defaults=request.defaults or None)
    resp.warnings = warnings + resp.warnings
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
        raise HTTPException(status_code=503, detail="Keycloak chưa cấu hình.")

    users, warnings = _resolve_users_list(request.job_id, request.users)
    if not users:
        detail = "Không có user nào để xử lý."
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
        raise HTTPException(status_code=404, detail=f"Không tìm thấy job: {job_id}")
    users, warnings = map_result_to_users(result)
    return UserPreviewResponse(job_id=job_id, total=len(users), users=users, warnings=warnings)
