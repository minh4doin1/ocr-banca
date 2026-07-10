"""
Keycloak Admin Proxy — FastAPI entrypoint.

Mọi request bắt đầu bằng `settings.proxy_path_prefix` (mặc định
`/api/v1/iam-bridge`) sẽ được forward tới Keycloak admin API
(`/admin/realms/{realm}/<phần sau prefix>`).

Ví dụ:
    POST /api/v1/iam-bridge/users
      → POST http://keycloak.../admin/realms/agribank/users

Docs/Redoc/OpenAPI schema bị ẩn (`docs_url=None`) để path /docs không
lộ thông tin kiến trúc.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.audit import Timer, log_request
from app.auth import require_proxy_key
from app.config import settings
from app.keycloak_client import KeycloakProxyError, kc_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="KC Admin Proxy",
    description=(
        "Proxy trung gian — forward Keycloak Admin API. "
        "Chạy trong cluster, gọi Keycloak qua cluster DNS nội bộ (không qua F5)."
    ),
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Kubernetes readiness/liveness probe."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz() -> dict:
    """Sẵn sàng khi config Keycloak đầy đủ."""
    if not (
        settings.keycloak_client_id and settings.keycloak_client_secret
    ):
        return JSONResponse(
            status_code=503,
            content={"status": "not-ready", "reason": "kc credentials missing"},
        )
    return {"status": "ready"}


# ── Catch-all forward ──

@app.api_route(
    f"{settings.proxy_path_prefix}/{{kc_path:path}}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def forward_to_keycloak(kc_path: str, request: Request) -> Response:
    """
    Forward request tới Keycloak admin API.

    Headers:
        X-Proxy-Key: shared secret (bắt buộc khi PROXY_API_KEY đã set)
        X-Request-Id: optional, dùng để correlation log

    Body/params: pass-through nguyên xi.
    """
    # 1. Auth
    require_proxy_key(request.headers.get("x-proxy-key"))

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    body = await request.body()

    # 2. Forward
    try:
        with Timer() as t:
            upstream = kc_client.forward(
                method=request.method,
                path=f"/{kc_path}",
                params=dict(request.query_params),
                body=body,
                content_type=request.headers.get("content-type"),
            )
    except KeycloakProxyError as exc:
        logger.warning("rid=%s upstream error: %s", request_id, exc)
        if settings.audit_log_enabled:
            log_request(
                request_id=request_id,
                source_ip=request.client.host if request.client else "?",
                method=request.method,
                kc_path=f"/{kc_path}",
                status=502,
                latency_ms=0.0,
            )
        return JSONResponse(
            status_code=502,
            content={"error": "upstream-unreachable", "detail": str(exc)},
        )

    # 3. Audit log
    if settings.audit_log_enabled:
        log_request(
            request_id=request_id,
            source_ip=request.client.host if request.client else "?",
            method=request.method,
            kc_path=f"/{kc_path}",
            status=upstream.status_code,
            latency_ms=t.elapsed_ms,
        )

    # 4. Pass-through response (giữ status + body + content-type)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"X-Request-Id": request_id},
    )