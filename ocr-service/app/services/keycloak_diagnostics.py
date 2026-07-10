"""Chẩn đoán kết nối Keycloak từng bước (dev/prod)."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import requests

from app.config import settings
from app.models.schemas import KeycloakDiagStep, KeycloakDiagnosticsResponse
from app.services.keycloak_env import resolve_keycloak_profile
from app.services.keycloak_service import KeycloakClient, KeycloakError, _safe_body


def _step(
    name: str,
    ok: bool,
    message: str = "",
    detail: str = "",
    **extra,
) -> KeycloakDiagStep:
    return KeycloakDiagStep(
        step=name,
        ok=ok,
        message=message,
        detail=detail[:800],
        status_code=extra.get("status_code"),
        content_type=extra.get("content_type", ""),
    )


def run_keycloak_diagnostics(target_env: str) -> KeycloakDiagnosticsResponse:
    """Chạy battery test Keycloak — không lộ secret."""
    kc = resolve_keycloak_profile(target_env)
    resp = KeycloakDiagnosticsResponse(
        target_env=kc.env,
        base_url=kc.base_url,
        realm=kc.realm,
        provision_client_id=kc.client_id,
        roles_client_id=kc.roles_client_id,
        roles_client_uuid_configured=bool(kc.roles_client_uuid.strip()),
        verify_ssl=kc.verify_ssl,
    )

    if not kc.configured:
        resp.steps.append(
            _step("config", False, f"Keycloak {kc.env.upper()} chưa cấu hình đủ")
        )
        resp.ok = False
        resp.summary = "Thiếu KEYCLOAK_* hoặc KEYCLOAK_PROD_* trong .env"
        return resp

    host = urlparse(kc.base_url).hostname or ""
    try:
        ip = socket.gethostbyname(host)
        resp.steps.append(
            _step("dns", True, f"{host} → {ip}", detail=f"resolved_ip={ip}")
        )
    except OSError as exc:
        resp.steps.append(
            _step("dns", False, f"Không resolve được {host}", detail=str(exc))
        )
        resp.ok = False
        resp.summary = f"DNS/hosts: thêm 10.0.93.20 {host} vào hosts"
        return resp

    timeout = settings.keycloak_timeout_seconds
    try:
        client = KeycloakClient(
            base_url=kc.base_url,
            realm=kc.realm,
            client_id=kc.client_id,
            client_secret=kc.client_secret,
            verify_ssl=kc.verify_ssl,
            timeout=timeout,
        )
        tok = client.get_access_token()
        resp.steps.append(
            _step(
                "token",
                True,
                "Lấy access token OK",
                detail=f"token_length={len(tok)}",
            )
        )
    except KeycloakError as exc:
        resp.steps.append(_step("token", False, "Lấy token thất bại", detail=str(exc)))
        resp.ok = False
        resp.summary = str(exc)[:300]
        return resp

    headers = {"Authorization": f"Bearer {client.get_access_token()}"}
    admin = f"{kc.base_url}/admin/realms/{kc.realm}"

    def _probe_get(
        step_name: str, path: str, *, params: dict | None = None
    ) -> requests.Response | None:
        url = admin + path
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers,
                verify=kc.verify_ssl,
                timeout=timeout,
            )
            ctype = (r.headers.get("content-type") or "")[:60]
            body = _safe_body(r)
            low = body.lower()
            html_block = (
                "html" in ctype.lower()
                or low.startswith("waf")
                or "request rejected" in low
            )
            ok = r.status_code == 200 and not html_block
            resp.steps.append(
                _step(
                    step_name,
                    ok,
                    f"HTTP {r.status_code}"
                    + (" (WAF/HTML)" if not ok and r.status_code == 200 else ""),
                    detail=body[:400],
                    status_code=r.status_code,
                    content_type=ctype,
                )
            )
            return r
        except requests.RequestException as exc:
            resp.steps.append(
                _step(step_name, False, f"Lỗi kết nối {path}", detail=str(exc))
            )
            return None

    _probe_get("users_list", "/users", params={"max": "1"})
    _probe_get(
        "users_search",
        "/users",
        params={"username": "test@agribank.com.vn", "exact": "true"},
    )
    _probe_get(
        "clients_lookup",
        "/clients",
        params={"clientId": kc.roles_client_id},
    )

    if kc.roles_client_uuid.strip():
        uuid = kc.roles_client_uuid.strip()
        _probe_get("role_lookup", f"/clients/{uuid}/roles/banca-seller")
        resp.steps.append(
            _step(
                "roles_uuid_config",
                True,
                f"Dùng UUID cấu hình: {uuid[:8]}…",
                detail="Bỏ qua GET /clients?clientId=…",
            )
        )
    else:
        resp.steps.append(
            _step(
                "roles_uuid_config",
                False,
                "Chưa có KEYCLOAK_PROD_ROLES_CLIENT_UUID",
                detail="WAF chặn /clients → cần điền UUID client banca thủ công",
            )
        )

    failed = [s for s in resp.steps if not s.ok]
    if not failed:
        resp.ok = True
        resp.summary = f"Keycloak {kc.env.upper()} — tất cả bước kiểm tra OK"
    else:
        resp.ok = False
        names = ", ".join(s.step for s in failed)
        resp.summary = f"Keycloak {kc.env.upper()} — lỗi tại: {names}"

    return resp
