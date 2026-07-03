"""FastAPI dependencies for OCR service."""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import settings


def verify_worker_token(authorization: str | None = Header(default=None)) -> None:
    """
    Optional bearer token auth for remote worker endpoints.

    When REMOTE_WORKER_TOKEN is set in .env, all OCR API calls require
    Authorization: Bearer <token>.
    """
    expected = settings.remote_worker_token.strip()
    if not expected:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Thiếu Authorization header (Bearer token)",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Token worker không hợp lệ")
