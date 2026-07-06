"""
Users Router — Tạo lô user trên Keycloak qua Admin REST API.

Nguồn dữ liệu: kết quả OCR (job_id) hoặc danh sách JSON trực tiếp.
Xử lý user đã tồn tại theo hành động per-user (on_conflict):
  skip | reset_password | reset_otp | reset_both
"""

from __future__ import annotations

import logging
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.deps import verify_worker_token
from app.models.schemas import (
    BatchProvisionRequest,
    BatchProvisionResponse,
    ErrorResponse,
    KeycloakUserInput,
    OnConflictAction,
    ProvisionStatus,
    UserPreviewResponse,
    UserProvisionResult,
)
from app.services.keycloak_service import (
    REQUIRED_ACTION_CONFIGURE_TOTP,
    REQUIRED_ACTION_UPDATE_PASSWORD,
    KeycloakClient,
    KeycloakConflictError,
    KeycloakError,
)
from app.services.table_service import get_result
from app.services.user_mapping import map_result_to_users

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/users",
    tags=["Users"],
    dependencies=[Depends(verify_worker_token)],
)


def _generate_temp_password(length: int = 14) -> str:
    """Sinh mật khẩu tạm mạnh (đủ chữ hoa/thường/số/ký tự đặc biệt)."""
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
    """Chọn mật khẩu tạm: input -> cấu hình mặc định -> sinh ngẫu nhiên."""
    if user.password:
        return user.password
    if settings.keycloak_default_temp_password:
        return settings.keycloak_default_temp_password
    return _generate_temp_password()


def _build_client(realm: str) -> KeycloakClient:
    try:
        return KeycloakClient(realm=realm or None)
    except KeycloakError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def _provision_one(
    client: KeycloakClient,
    user: KeycloakUserInput,
    *,
    temporary: bool,
    on_conflict: OnConflictAction,
    default_required_actions: list[str],
) -> UserProvisionResult:
    """Xử lý một user: tạo mới hoặc xử lý trùng theo on_conflict."""
    username = user.username.strip()
    result = UserProvisionResult(username=username, status=ProvisionStatus.FAILED)

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
                first_name=user.first_name,
                last_name=user.last_name,
                password=_resolve_temp_password(user),
                temporary=temporary,
                required_actions=required_actions,
            )
            result.status = ProvisionStatus.CREATED
            result.user_id = user_id
            result.actions_applied = ["create"] + list(required_actions)
            return result

        # User đã tồn tại — xử lý theo on_conflict
        user_id = str(existing.get("id", ""))
        result.user_id = user_id
        action = user.on_conflict or on_conflict

        if action == OnConflictAction.SKIP:
            result.status = ProvisionStatus.SKIPPED
            result.actions_applied = ["skip"]
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
            if REQUIRED_ACTION_CONFIGURE_TOTP not in applied:
                applied.append(REQUIRED_ACTION_CONFIGURE_TOTP)

        result.status = ProvisionStatus.UPDATED
        result.actions_applied = applied
        return result

    except KeycloakConflictError as exc:
        # Hiếm gặp: race condition giữa search và create
        result.status = ProvisionStatus.FAILED
        result.error = str(exc)
        return result
    except KeycloakError as exc:
        result.status = ProvisionStatus.FAILED
        result.error = str(exc)
        logger.warning("Provision user '%s' lỗi: %s", username, exc)
        return result


@router.post(
    "/provision-batch",
    response_model=BatchProvisionResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Tạo lô user trên Keycloak (từ job OCR hoặc danh sách JSON)",
)
async def provision_batch(request: BatchProvisionRequest):
    """Tạo lô user; xử lý user đã tồn tại theo on_conflict (per-user)."""
    if not settings.keycloak_configured and not request.realm:
        raise HTTPException(
            status_code=503,
            detail=(
                "Keycloak chưa được cấu hình. Đặt KEYCLOAK_BASE_URL/REALM/"
                "CLIENT_ID/CLIENT_SECRET trong .env."
            ),
        )

    users, warnings = _resolve_users(request)
    if not users:
        detail = "Không có user nào để xử lý."
        if warnings:
            detail += " " + " ".join(warnings)
        raise HTTPException(status_code=400, detail=detail)

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

    client = _build_client(request.realm)

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

    logger.info(
        "Provision batch: total=%d created=%d updated=%d skipped=%d failed=%d",
        response.total,
        response.created,
        response.updated,
        response.skipped,
        response.failed,
    )
    return response


@router.get(
    "/preview-from-job/{job_id}",
    response_model=UserPreviewResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Xem trước danh sách user được map từ job OCR",
)
async def preview_from_job(job_id: str):
    """Map kết quả OCR sang danh sách user để review trước khi tạo lô."""
    result = get_result(job_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Không tìm thấy kết quả OCR cho job: {job_id}"
        )
    users, warnings = map_result_to_users(result)
    return UserPreviewResponse(
        job_id=job_id,
        total=len(users),
        users=users,
        warnings=warnings,
    )


def _resolve_users(
    request: BatchProvisionRequest,
) -> tuple[list[KeycloakUserInput], list[str]]:
    """Lấy danh sách user từ job_id (ưu tiên) hoặc từ request.users."""
    if request.job_id:
        result = get_result(request.job_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"Không tìm thấy kết quả OCR cho job: {request.job_id}",
            )
        return map_result_to_users(result)
    return list(request.users), []
