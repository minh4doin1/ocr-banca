"""
Templates Router — CRUD + upload sample for multi-system OCR templates.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.deps import verify_worker_token
from app.models.schemas import (
    AvailableActionsResponse,
    AvailableFieldsResponse,
    ErrorResponse,
    TemplateInferResponse,
    TemplateListResponse,
    TemplateProfile,
    TemplateUpdateRequest,
)
from app.services.action_registry import list_available_actions
from app.services import template_service as tpl

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/templates",
    tags=["Templates"],
    dependencies=[Depends(verify_worker_token)],
)

ALLOWED_SAMPLE_EXTENSIONS = {".xlsx", ".xlsm", ".docx", ".pdf"}
MAX_SAMPLE_SIZE = 50 * 1024 * 1024


@router.get("", response_model=TemplateListResponse, summary="Danh sách template")
async def list_templates():
    templates = tpl.list_templates()
    return TemplateListResponse(
        templates=templates,
        default_id=tpl.DEFAULT_TEMPLATE_ID,
    )


@router.get(
    "/actions",
    response_model=AvailableActionsResponse,
    summary="Danh sách action sau OCR",
)
async def get_available_actions():
    return AvailableActionsResponse(actions=list_available_actions())


@router.get(
    "/fields",
    response_model=AvailableFieldsResponse,
    summary="Danh sách field nội bộ có thể map",
)
async def get_available_fields():
    return AvailableFieldsResponse(fields=tpl.list_available_fields())


@router.get(
    "/{template_id}",
    response_model=TemplateProfile,
    responses={404: {"model": ErrorResponse}},
    summary="Chi tiết template",
)
async def get_template(template_id: str):
    profile = tpl.get_template(template_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Template không tồn tại: {template_id}")
    return profile


@router.post(
    "/upload",
    response_model=TemplateInferResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}},
    summary="Upload file mẫu → suy ra draft template",
)
async def upload_sample(
    file: UploadFile = File(..., description="File mẫu Excel/Word/PDF"),
    name: str = Form(default=""),
    template_id: str = Form(default=""),
    save: str = Form(default="false"),
):
    """
    Upload sample, infer column headers + suggested field mapping.

    If save=true, persist the draft as a new template (after storing the sample).
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_SAMPLE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Chỉ hỗ trợ .xlsx/.xlsm/.docx/.pdf. Nhận được: {ext}",
        )

    content = await file.read()
    if len(content) > MAX_SAMPLE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File quá lớn. Tối đa: {MAX_SAMPLE_SIZE // (1024 * 1024)} MB",
        )

    tmp_dir = tpl._samples_dir() / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"infer_{file.filename}"
    async with aiofiles.open(tmp_path, "wb") as f:
        await f.write(content)

    try:
        draft, warnings = tpl.infer_from_sample(
            tmp_path, name=name, template_id=template_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    should_save = save.strip().lower() in ("true", "1", "yes", "on")
    if should_save:
        if tpl.get_template(draft.id) and draft.id == tpl.DEFAULT_TEMPLATE_ID:
            raise HTTPException(
                status_code=400,
                detail="Không được ghi đè template built-in sso-agribank",
            )
        rel = tpl.store_sample_file(content, file.filename, draft.id)
        draft.source_sample = rel
        try:
            draft = tpl.save_template(draft, overwrite=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TemplateInferResponse(draft=draft, warnings=warnings)


@router.post(
    "",
    response_model=TemplateProfile,
    responses={400: {"model": ErrorResponse}},
    summary="Tạo / ghi đè template từ JSON",
)
async def create_template(profile: TemplateProfile):
    try:
        if profile.id == tpl.DEFAULT_TEMPLATE_ID and profile.builtin is False:
            # Protect builtin id
            existing = tpl.get_template(profile.id)
            if existing and existing.builtin:
                raise ValueError("Không được ghi đè id built-in bằng template thường")
        return tpl.save_template(profile, overwrite=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/{template_id}",
    response_model=TemplateProfile,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Cập nhật template",
)
async def update_template(template_id: str, request: TemplateUpdateRequest):
    try:
        return tpl.update_template(template_id, request)
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "không tồn tại" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc


@router.delete(
    "/{template_id}",
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Xóa template (không xóa built-in)",
)
async def delete_template(template_id: str):
    try:
        tpl.delete_template(template_id)
    except ValueError as exc:
        msg = str(exc)
        status = 404 if "không tồn tại" in msg.lower() else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    return {"ok": True, "id": template_id}
