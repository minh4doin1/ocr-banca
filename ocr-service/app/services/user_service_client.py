"""
UserServiceClient — HTTP client tới user-service (Node.js BE).

Sau khi user-service triển khai, OCR service KHÔNG gọi trực tiếp Keycloak
Admin API nữa — toàn bộ qua user-service. Service này wrap HTTP call
thành interface giống `KeycloakClient` cũ để router dễ refactor.

So sánh:
    KeycloakClient.create_user(...)        → user_service_client.create_user(...)
    KeycloakClient.reset_password(...)    → user_service_client.reset_password(...)
    KeycloakClient.assign_client_roles..  → user_service_client.assign_roles(...)

Ưu điểm so với gọi Keycloak trực tiếp:
    - Không cần biết UUID (user-service tự resolve)
    - Không cần handle 409 / 404 riêng (user-service map sẵn)
    - Không cần retry với client khác (user-service dùng 1 client duy nhất)
    - Auth tập trung (X-Service-Token)
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ── Errors ──


class UserServiceError(Exception):
    """Lỗi khi giao tiếp với user-service."""


class UserServiceAuthError(UserServiceError):
    """401/403 từ user-service (token sai / thiếu)."""


class UserServiceUnavailableError(UserServiceError):
    """502/503/504 — user-service không reach được hoặc upstream Keycloak lỗi."""


class UserConflictError(UserServiceError):
    """409 — user đã tồn tại (HTTP 409)."""


class UserNotFoundError(UserServiceError):
    """404 — user không tồn tại."""

    def __init__(self, user_id: str):
        super().__init__(f"User '{user_id}' không tồn tại.")
        self.user_id = user_id


# ── Client ──


class UserServiceClient:
    """
    HTTP client mỏng tới user-service.

    Không cache gì — user-service đã cache UUID + token internally.
    Session được dùng lại (connection pooling).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 30,
        roles_client_id: str = "banca",
    ) -> None:
        if not base_url:
            raise UserServiceError("USER_SERVICE_URL chưa cấu hình.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.roles_client_id = roles_client_id
        self._session = requests.Session()

    # ── HTTP helpers ──

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Service-Token"] = self.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | list | None = None,
        params: dict | None = None,
        allow_404: bool = False,
    ) -> requests.Response | None:
        """Low-level HTTP call. Map status code → exception."""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise UserServiceUnavailableError(
                f"Không kết nối được user-service {method} {path}: {exc}"
            ) from exc

        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code in (401, 403):
            raise UserServiceAuthError(
                f"user-service auth lỗi ({method} {path}): {resp.text[:200]}"
            )
        if resp.status_code == 409:
            raise UserConflictError(
                f"User đã tồn tại ({method} {path}): {resp.text[:200]}"
            )
        if resp.status_code == 404:
            raise UserNotFoundError(path)
        if resp.status_code >= 500:
            raise UserServiceUnavailableError(
                f"user-service upstream lỗi ({method} {path}, HTTP {resp.status_code}): "
                f"{resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise UserServiceError(
                f"user-service trả lỗi ({method} {path}, HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )
        return resp

    # ── Users ──

    def find_user_by_username(self, username: str) -> dict | None:
        """Trả user dict nếu tìm thấy, None nếu không."""
        resp = self._request(
            "GET", f"/users/by-username/{username}", allow_404=True
        )
        if resp is None:
            return None
        body = resp.json()
        if body.get("found"):
            return body.get("user")
        return None

    def get_user(self, user_id: str) -> dict | None:
        resp = self._request("GET", f"/users/{user_id}", allow_404=True)
        if resp is None:
            return None
        return resp.json()

    def create_user(
        self,
        *,
        username: str,
        email: str = "",
        first_name: str = "",
        last_name: str = "",
        password: str = "",
        temporary: bool = True,
        required_actions: list[str] | None = None,
        enabled: bool = True,
        attributes: dict | None = None,
    ) -> str:
        payload: dict[str, Any] = {"username": username, "enabled": enabled}
        if email:
            payload["email"] = email
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if password:
            payload["password"] = password
            payload["temporary"] = temporary
        if required_actions:
            payload["requiredActions"] = required_actions
        if attributes:
            payload["attributes"] = attributes

        resp = self._request("POST", "/users", json=payload)
        body = resp.json()
        return body["id"]

    def update_user_details(
        self,
        user_id: str,
        *,
        email: str = "",
        first_name: str = "",
        last_name: str = "",
    ) -> None:
        payload: dict[str, Any] = {}
        if email:
            payload["email"] = email
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if not payload:
            return
        self._request("PUT", f"/users/{user_id}", json=payload)

    def reset_password(
        self, user_id: str, password: str, temporary: bool = True
    ) -> None:
        self._request(
            "PUT",
            f"/users/{user_id}/password",
            json={"password": password, "temporary": temporary},
        )

    def reset_otp(self, user_id: str) -> int:
        """Trả số credential OTP đã xóa."""
        resp = self._request("POST", f"/users/{user_id}/otp/reset")
        return int(resp.json().get("deleted", 0))

    def ensure_required_actions(
        self, user_id: str, actions: list[str]
    ) -> list[str]:
        resp = self._request(
            "POST", f"/users/{user_id}/required-actions", json=actions
        )
        return resp.json().get("requiredActions", [])

    def set_required_actions(
        self, user_id: str, actions: list[str]
    ) -> list[str]:
        resp = self._request(
            "PUT", f"/users/{user_id}/required-actions", json=actions
        )
        return resp.json().get("requiredActions", [])

    def update_user_attributes(
        self, user_id: str, attributes: dict[str, list[str]]
    ) -> None:
        """Merge attributes (không ghi đè)."""
        if not attributes:
            return
        self._request("PUT", f"/users/{user_id}/attributes", json=attributes)

    # ── Roles ──

    def get_user_client_roles(
        self, user_id: str, client_id: str | None = None
    ) -> list[dict]:
        cid = client_id or self.roles_client_id
        resp = self._request(
            "GET", f"/users/{user_id}/roles", params={"clientId": cid}
        )
        return resp.json().get("roles", [])

    def assign_roles(
        self,
        user_id: str,
        role_names: list[str],
        client_id: str | None = None,
    ) -> dict[str, list[str]]:
        """Trả {assigned: [...], skipped: [...]}."""
        if not role_names:
            return {"assigned": [], "skipped": []}
        cid = client_id or self.roles_client_id
        resp = self._request(
            "POST", f"/users/{user_id}/roles", params={"clientId": cid}, json=role_names
        )
        return resp.json()

    def remove_roles(
        self,
        user_id: str,
        role_names: list[str],
        client_id: str | None = None,
    ) -> dict[str, list[str]]:
        """Trả {removed: [...], skipped: [...]}."""
        if not role_names:
            return {"removed": [], "skipped": []}
        cid = client_id or self.roles_client_id
        resp = self._request(
            "DELETE", f"/users/{user_id}/roles", params={"clientId": cid}, json=role_names
        )
        return resp.json()

    # ── Health ──

    def health(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.base_url}/healthz", timeout=self.timeout
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False