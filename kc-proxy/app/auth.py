"""
Auth giữa OCR service ↔ proxy.

Dùng shared secret qua header `X-Proxy-Key`. Constant-time compare để
chống timing attack. Khi `PROXY_API_KEY` rỗng thì auth bị tắt (chỉ dev).
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def require_proxy_key(x_proxy_key: str | None = Header(default=None)) -> None:
    """Validate X-Proxy-Key. 401 nếu sai hoặc thiếu."""
    expected = settings.proxy_api_key
    if not expected:
        # Dev mode — KHÔNG deploy production với config này
        return
    if not x_proxy_key or not hmac.compare_digest(x_proxy_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing proxy key",
        )