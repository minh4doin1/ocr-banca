"""
Keycloak client — token cache + forward request.

Token lấy qua client_credentials grant, cache theo expires_in.
Mọi request tới /admin/realms/{realm}/* được forward kèm Bearer token.

Lưu ý: traffic proxy → Keycloak đi qua cluster DNS nội bộ, không qua F5/WAF.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class KeycloakProxyError(Exception):
    """Lỗi khi proxy không reach được Keycloak."""


class KeycloakAdminClient:
    """Client mỏng, thread-safe, cache token."""

    def __init__(self) -> None:
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    # ── Token ──

    def _token_url(self) -> str:
        return (
            f"{settings.keycloak_internal_url}"
            f"/realms/{settings.keycloak_realm}"
            "/protocol/openid-connect/token"
        )

    def _admin_url(self, path: str) -> str:
        return (
            f"{settings.keycloak_internal_url}"
            f"/admin/realms/{settings.keycloak_realm}{path}"
        )

    def _get_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_expiry:
                return self._token

            try:
                resp = httpx.post(
                    self._token_url(),
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.keycloak_client_id,
                        "client_secret": settings.keycloak_client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    verify=settings.keycloak_verify_ssl,
                    timeout=settings.keycloak_timeout_seconds,
                )
            except httpx.RequestError as exc:
                raise KeycloakProxyError(
                    f"Không kết nối được Keycloak token endpoint: {exc}"
                ) from exc

            if resp.status_code != 200:
                raise KeycloakProxyError(
                    f"Lấy token thất bại (HTTP {resp.status_code}): {resp.text[:300]}"
                )

            data = resp.json()
            self._token = data.get("access_token", "")
            if not self._token:
                raise KeycloakProxyError("Token endpoint không trả về access_token.")

            expires_in = int(data.get("expires_in", 60))
            self._token_expiry = now + max(
                expires_in - settings.keycloak_token_leeway_seconds, 5
            )
            return self._token

    # ── Forward ──

    def forward(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> httpx.Response:
        """
        Forward 1 request tới Keycloak admin API.

        Trả về `httpx.Response` để caller quyết định status/body trả về client.
        """
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type

        try:
            resp = httpx.request(
                method,
                self._admin_url(path),
                params=params,
                content=body if body else None,
                headers=headers,
                verify=settings.keycloak_verify_ssl,
                timeout=settings.keycloak_timeout_seconds,
            )
        except httpx.RequestError as exc:
            raise KeycloakProxyError(
                f"Lỗi gọi Keycloak {method} {path}: {exc}"
            ) from exc

        return resp


kc_client = KeycloakAdminClient()