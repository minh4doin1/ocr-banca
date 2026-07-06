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
    map_result_to_users,
    validate_user_fields,
)

logger = logging.getLogger(__name__)

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


def _enrich_users(users: list[KeycloakUserInput]) -> EnrichResponse:
    client = get_client()
    warnings: list[str] = []
    enriched: list[KeycloakUserInput] = []

    for user in users:
        row = enrich_user_row(user.model_dump(), client)
        updated = KeycloakUserInput.model_validate(row)
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
    username = user.username.strip()
    result = UserProvisionResult(username=username, status=ProvisionStatus.FAILED)

    missing = validate_user_fields(user)
    if missing:
        result.error = f"Thiếu trường bắt buộc: {', '.join(missing)}"
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
                email=user.email,
                first_name=first,
                last_name=last,
                password=_resolve_temp_password(user),
                temporary=temporary,
                required_actions=required_actions,
                attributes=attrs,
            )
            result.status = ProvisionStatus.CREATED
            result.user_id = user_id
            result.actions_applied = ["create"] + list(required_actions)
            if attrs:
                result.actions_applied.append("set_attributes")
            return result

        user_id = str(existing.get("id", ""))
        result.user_id = user_id
        action = user.on_conflict or on_conflict

        if action == OnConflictAction.SKIP:
            if attrs:
                client.update_user_attributes(user_id, attrs)
                result.actions_applied = ["skip", "set_attributes"]
            else:
                result.actions_applied = ["skip"]
            result.status = ProvisionStatus.SKIPPED
            return result

        applied: list[str] = []
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

        if attrs:
            client.update_user_attributes(user_id, attrs)
            applied.append("set_attributes")

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
    return FieldConfigResponse(
        required_fields=settings.user_required_fields_list,
        header_map=settings.keycloak_header_map_parsed,
        banca_core_enabled=settings.banca_core_configured,
    )


@router.post("/enrich", response_model=EnrichResponse)
async def enrich_users(request: EnrichRequest):
    users, warnings = _resolve_users_list(request.job_id, request.users)
    if not users:
        detail = "Không có user để enrich."
        if warnings:
            detail += " " + " ".join(warnings)
        raise HTTPException(status_code=400, detail=detail)
    resp = _enrich_users(users)
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
