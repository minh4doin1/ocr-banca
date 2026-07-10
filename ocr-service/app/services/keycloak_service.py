"""
Keycloak Service — Quản lý User qua Admin REST API.

Dùng Service Account (grant type client_credentials) — KHÔNG dùng tài khoản
admin hay admin-cli. Client chỉ cần client_id + client_secret với quyền tối
thiểu realm-management: manage-users, view-users, view-clients, manage-clients.

Tham chiếu: Keycloak 24 Admin REST API
  - POST /realms/{realm}/protocol/openid-connect/token   (client_credentials)
  - GET  /admin/realms/{realm}/users?username=..&exact=true
  - POST /admin/realms/{realm}/users
  - PUT  /admin/realms/{realm}/users/{id}
  - PUT  /admin/realms/{realm}/users/{id}/reset-password
  - GET  /admin/realms/{realm}/users/{id}/credentials
  - DELETE /admin/realms/{realm}/users/{id}/credentials/{credentialId}
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from app.config import settings

logger = logging.getLogger(__name__)

REQUIRED_ACTION_UPDATE_PASSWORD = "UPDATE_PASSWORD"
REQUIRED_ACTION_CONFIGURE_TOTP = "CONFIGURE_TOTP"
OTP_CREDENTIAL_TYPE = "otp"


class KeycloakError(Exception):
    """Lỗi khi giao tiếp với Keycloak Admin REST API."""


class KeycloakConflictError(KeycloakError):
    """User đã tồn tại (HTTP 409) khi tạo mới."""


class KeycloakClient:
    """
    Client mỏng bọc Admin REST API của Keycloak, có cache access token.

    Thread-safe cho việc lấy/refresh token (dùng lock) vì batch có thể chạy
    trong background thread.
    """

    def __init__(
        self,
        base_url: str | None = None,
        realm: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        verify_ssl: bool | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.keycloak_base_url).rstrip("/")
        self.realm = realm or settings.keycloak_realm
        self.client_id = client_id or settings.keycloak_client_id
        self.client_secret = client_secret or settings.keycloak_client_secret
        self.verify_ssl = (
            settings.keycloak_verify_ssl if verify_ssl is None else verify_ssl
        )
        self.timeout = timeout or settings.keycloak_timeout_seconds

        if not (self.base_url and self.realm and self.client_id and self.client_secret):
            raise KeycloakError(
                "Keycloak chưa được cấu hình đầy đủ "
                "(KEYCLOAK_BASE_URL/REALM/CLIENT_ID/CLIENT_SECRET)."
            )

        self._session = requests.Session()
        self._token: str = ""
        self._token_expiry: float = 0.0
        self._token_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────
    # Token
    # ──────────────────────────────────────────────────────────

    def _token_url(self) -> str:
        return (
            f"{self.base_url}/realms/{self.realm}"
            "/protocol/openid-connect/token"
        )

    def _admin_url(self, path: str) -> str:
        return f"{self.base_url}/admin/realms/{self.realm}{path}"

    def get_access_token(self) -> str:
        """Lấy access token, tự cache và refresh theo expires_in."""
        with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expiry:
                return self._token

            try:
                resp = self._session.post(
                    self._token_url(),
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    verify=self.verify_ssl,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise KeycloakError(
                    f"Không kết nối được token endpoint: {exc}"
                ) from exc

            if resp.status_code != 200:
                _log_kc_response("POST", self._token_url(), resp)
                raise KeycloakError(
                    "Lấy access token thất bại "
                    f"(HTTP {resp.status_code}): {_safe_body(resp)}"
                )

            data = _parse_json(resp, context="Token endpoint")
            if not isinstance(data, dict):
                raise KeycloakError("Token endpoint trả về JSON không hợp lệ.")
            self._token = data.get("access_token", "")
            if not self._token:
                raise KeycloakError("Token endpoint không trả về access_token.")

            expires_in = int(data.get("expires_in", 60))
            leeway = settings.keycloak_token_leeway_seconds
            self._token_expiry = now + max(expires_in - leeway, 5)
            if settings.keycloak_debug:
                logger.debug(
                    "KC token OK realm=%s client=%s expires_in=%s",
                    self.realm,
                    self.client_id,
                    expires_in,
                )
            return self._token

    def _headers(self, json_body: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.get_access_token()}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        json_body: bool = True,
    ) -> requests.Response:
        if settings.keycloak_debug:
            logger.debug("KC → %s %s params=%s", method, url, params or "")
        try:
            resp = self._session.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers(json_body=json_body),
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("KC %s %s connection error: %s", method, url, exc)
            raise KeycloakError(f"Lỗi gọi Keycloak {method} {url}: {exc}") from exc

        _log_kc_response(method, url, resp, params=params)

        # F5/WAF đôi khi trả HTTP 200 + HTML "Request Rejected" thay vì JSON Admin API
        if _looks_like_html_block(resp):
            raise KeycloakError(
                f"Keycloak Admin API bi chan ({method} {url}): {_safe_body(resp)}"
            )
        return resp

    # ──────────────────────────────────────────────────────────
    # User operations
    # ──────────────────────────────────────────────────────────

    def find_user_by_username(self, username: str) -> dict | None:
        """Tìm user theo username khớp chính xác (exact=true)."""
        resp = self._request(
            "GET",
            self._admin_url("/users"),
            params={"username": username, "exact": "true"},
            json_body=False,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"Tìm user '{username}' thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        users = _parse_json(
            resp, context=f"Tìm user '{username}'"
        )
        if not isinstance(users, list) or not users:
            return None
        # exact=true có thể vẫn trả nhiều bản ghi; chọn đúng username.
        for user in users:
            if str(user.get("username", "")).lower() == username.lower():
                return user
        return users[0]

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
        attributes: dict[str, list[str]] | None = None,
    ) -> str:
        """
        Tạo user mới. Trả về userId (lấy từ header Location).

        Ném KeycloakConflictError nếu user đã tồn tại (HTTP 409).
        """
        payload: dict = {
            "username": username,
            "enabled": enabled,
        }
        if email:
            payload["email"] = email
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if password:
            payload["credentials"] = [
                {
                    "type": "password",
                    "value": password,
                    "temporary": temporary,
                }
            ]
        if required_actions:
            payload["requiredActions"] = required_actions
        if attributes:
            payload["attributes"] = attributes

        resp = self._request("POST", self._admin_url("/users"), json=payload)

        if resp.status_code == 409:
            raise KeycloakConflictError(f"User '{username}' đã tồn tại.")
        if resp.status_code not in (201, 204):
            raise KeycloakError(
                f"Tạo user '{username}' thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )

        location = resp.headers.get("Location", "")
        if location:
            return location.rstrip("/").rsplit("/", 1)[-1]

        # Dự phòng: tra lại nếu không có Location header.
        user = self.find_user_by_username(username)
        if user and user.get("id"):
            return str(user["id"])
        raise KeycloakError(
            f"Đã tạo user '{username}' nhưng không lấy được userId."
        )

    def reset_password(
        self, user_id: str, value: str, temporary: bool = True
    ) -> None:
        """Đặt lại mật khẩu cho user."""
        resp = self._request(
            "PUT",
            self._admin_url(f"/users/{user_id}/reset-password"),
            json={"type": "password", "value": value, "temporary": temporary},
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Reset password (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )

    def get_credentials(self, user_id: str) -> list[dict]:
        """Lấy danh sách credential của user."""
        resp = self._request(
            "GET",
            self._admin_url(f"/users/{user_id}/credentials"),
            json_body=False,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy credentials (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        return _parse_json(resp, context=f"Lấy credentials (user {user_id})") or []

    def delete_credential(self, user_id: str, credential_id: str) -> None:
        """Xóa 1 credential theo id."""
        resp = self._request(
            "DELETE",
            self._admin_url(f"/users/{user_id}/credentials/{credential_id}"),
            json_body=False,
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Xóa credential {credential_id} (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )

    def reset_otp(self, user_id: str) -> int:
        """
        Reset OTP: xóa mọi credential type=otp rồi gán lại CONFIGURE_TOTP.

        Trả về số credential OTP đã xóa. Không tự sinh/không lưu OTP secret.
        """
        deleted = 0
        for cred in self.get_credentials(user_id):
            if str(cred.get("type", "")).lower() == OTP_CREDENTIAL_TYPE:
                cred_id = cred.get("id")
                if cred_id:
                    self.delete_credential(user_id, str(cred_id))
                    deleted += 1
        self.set_required_actions(user_id, [REQUIRED_ACTION_CONFIGURE_TOTP])
        return deleted


    def set_required_actions(self, user_id: str, actions: list[str]) -> list[str]:
        """Replace required actions (used after OTP reset)."""
        resp = self._request(
            "PUT",
            self._admin_url(f"/users/{user_id}"),
            json={"requiredActions": list(actions)},
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Cap nhat requiredActions that bai (HTTP {resp.status_code})"
            )
        return list(actions)

    def ensure_required_actions(
        self, user_id: str, actions: list[str]
    ) -> list[str]:
        """
        Merge thêm required actions cho user (không ghi đè action đang có).

        GET user -> hợp nhất requiredActions -> PUT cập nhật.
        """
        resp = self._request(
            "GET",
            self._admin_url(f"/users/{user_id}"),
            json_body=False,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy user {user_id} thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        user = _parse_json(resp, context=f"Lấy user {user_id}")
        if not isinstance(user, dict):
            raise KeycloakError(f"Lấy user {user_id}: JSON không hợp lệ.")
        existing = list(user.get("requiredActions") or [])
        merged = list(dict.fromkeys(existing + actions))
        if merged == existing:
            return merged

        update_resp = self._request(
            "PUT",
            self._admin_url(f"/users/{user_id}"),
            json={"requiredActions": merged},
        )
        if update_resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Cập nhật requiredActions (user {user_id}) thất bại "
                f"(HTTP {update_resp.status_code}): {_safe_body(update_resp)}"
            )
        return merged

    def update_user_attributes(
        self, user_id: str, attributes: dict[str, list[str]]
    ) -> None:
        """Merge attributes vào user hiện có."""
        if not attributes:
            return
        resp = self._request(
            "GET",
            self._admin_url(f"/users/{user_id}"),
            json_body=False,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy user {user_id} thất bại (HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        user = _parse_json(resp, context=f"Lấy user {user_id}")
        if not isinstance(user, dict):
            raise KeycloakError(f"Lấy user {user_id}: JSON không hợp lệ.")
        existing = dict(user.get("attributes") or {})
        for key, vals in attributes.items():
            if vals:
                existing[key] = vals
        base_payload = {
            "username": user.get("username", ""),
            "email": user.get("email", ""),
            "firstName": user.get("firstName", ""),
            "lastName": user.get("lastName", ""),
            "enabled": user.get("enabled", True),
        }
        update_resp = self._request(
            "PUT",
            self._admin_url(f"/users/{user_id}"),
            json={**base_payload, "attributes": existing},
        )
        if update_resp.status_code in (200, 204):
            return

        # Some strict User Profile realms reject unknown keys. Retry with
        # required compatibility keys only.
        if update_resp.status_code == 400:
            strict_keys = {"branchId", "phone", "idNo"}
            strict_attrs = {
                k: v for k, v in attributes.items() if k in strict_keys and v
            }
            if strict_attrs:
                retry_resp = self._request(
                    "PUT",
                    self._admin_url(f"/users/{user_id}"),
                    json={**base_payload, "attributes": strict_attrs},
                )
                if retry_resp.status_code in (200, 204):
                    return
                raise KeycloakError(
                    f"Cập nhật attributes (strict fallback, user {user_id}) thất bại "
                    f"(HTTP {retry_resp.status_code}): {_safe_body(retry_resp)}"
                )

        raise KeycloakError(
            f"Cập nhật attributes (user {user_id}) thất bại "
            f"(HTTP {update_resp.status_code}): {_safe_body(update_resp)}"
        )

    # ──────────────────────────────────────────────────────────
    # Client role operations
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _role_mapping_payload(role_repr: dict, client_uuid: str) -> dict:
        """Payload tối thiểu cho POST role-mappings/clients."""
        return {
            "id": str(role_repr.get("id", "")),
            "name": str(role_repr.get("name", "")),
            "clientRole": True,
            "containerId": client_uuid,
        }

    def get_service_account_user(self, client_uuid: str) -> dict | None:
        """Lấy service-account user của một client (để chẩn đoán quyền)."""
        resp = self._request(
            "GET",
            self._admin_url(f"/clients/{client_uuid}/service-account-user"),
            json_body=False,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy service-account-user thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        data = _parse_json(resp, context="Lấy service-account-user")
        return data if isinstance(data, dict) else None

    def get_client_by_client_id(self, client_id: str) -> dict | None:
        """Tra client theo public clientId, trả representation đầy đủ."""
        resp = self._request(
            "GET",
            self._admin_url("/clients"),
            params={"clientId": client_id},
            json_body=False,
        )
        if resp.status_code != 200:
            raise KeycloakError(
                f"Tra client '{client_id}' thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        clients = _parse_json(resp, context=f"Tra client '{client_id}'") or []
        if not isinstance(clients, list):
            raise KeycloakError(f"Tra client '{client_id}': JSON không hợp lệ.")
        return clients[0] if clients else None

    def get_client_role(self, client_uuid: str, role_name: str) -> dict | None:
        resp = self._request(
            "GET",
            self._admin_url(f"/clients/{client_uuid}/roles/{role_name}"),
            json_body=False,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy role '{role_name}' thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        data = _parse_json(resp, context=f"Lấy role '{role_name}'")
        return data if isinstance(data, dict) else None

    def get_user_client_roles_optional(
        self, user_id: str, client_uuid: str
    ) -> tuple[list[dict], str | None]:
        """Trả (roles, error). error=None khi OK; '403' khi forbidden."""
        resp = self._request(
            "GET",
            self._admin_url(
                f"/users/{user_id}/role-mappings/clients/{client_uuid}"
            ),
            json_body=False,
        )
        if resp.status_code == 403:
            return [], "403"
        if resp.status_code != 200:
            raise KeycloakError(
                f"Lấy client roles (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        roles = _parse_json(resp, context=f"Lấy client roles (user {user_id})") or []
        if not isinstance(roles, list):
            raise KeycloakError(f"Lấy client roles (user {user_id}): JSON không hợp lệ.")
        return roles, None

    def get_user_client_roles(self, user_id: str, client_uuid: str) -> list[dict]:
        roles, err = self.get_user_client_roles_optional(user_id, client_uuid)
        if err:
            raise KeycloakError(
                f"Lấy client roles (user {user_id}) thất bại (HTTP {err})"
            )
        return roles

    def assign_client_roles_batch(
        self, user_id: str, client_uuid: str, roles: list[dict]
    ) -> None:
        if not roles:
            return
        resp = self._request(
            "POST",
            self._admin_url(
                f"/users/{user_id}/role-mappings/clients/{client_uuid}"
            ),
            json=roles,
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Gán client role (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )

    def assign_client_roles(
        self, user_id: str, client_uuid: str, roles: list[dict]
    ) -> None:
        self.assign_client_roles_batch(user_id, client_uuid, roles)

    def remove_client_roles_batch(
        self, user_id: str, client_uuid: str, roles: list[dict]
    ) -> None:
        """Gỡ (thu hồi) danh sách client role khỏi user."""
        if not roles:
            return
        resp = self._request(
            "DELETE",
            self._admin_url(
                f"/users/{user_id}/role-mappings/clients/{client_uuid}"
            ),
            json=roles,
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Gỡ client role (user {user_id}) thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )

    def update_user_details(
        self,
        user_id: str,
        *,
        email: str = "",
        first_name: str = "",
        last_name: str = "",
    ) -> None:
        """Cập nhật thông tin General tab (Save Details)."""
        payload: dict = {}
        if email:
            payload["email"] = email
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if not payload:
            return
        resp = self._request(
            "PUT",
            self._admin_url(f"/users/{user_id}"),
            json=payload,
        )
        if resp.status_code not in (200, 204):
            raise KeycloakError(
                f"Cập nhật user {user_id} thất bại "
                f"(HTTP {resp.status_code}): {_safe_body(resp)}"
            )


def _log_kc_response(
    method: str,
    url: str,
    resp: requests.Response,
    *,
    params: dict | None = None,
) -> None:
    """Ghi log chi tiết khi KEYCLOAK_DEBUG hoặc response lỗi/WAF."""
    blocked = _looks_like_html_block(resp)
    if not settings.keycloak_debug and resp.status_code < 400 and not blocked:
        return
    ctype = (resp.headers.get("content-type") or "")[:80]
    body = _safe_body(resp)
    level = (
        logging.WARNING
        if resp.status_code >= 400 or blocked
        else logging.DEBUG
    )
    logger.log(
        level,
        "KC %s %s status=%s ctype=%s params=%s body=%s",
        method,
        url,
        resp.status_code,
        ctype,
        params or "",
        body[:600],
    )


def _safe_body(resp: requests.Response) -> str:
    """Trích nội dung lỗi ngắn gọn, không lộ dữ liệu nhạy cảm."""
    try:
        text = resp.text or ""
    except Exception:
        return "<no body>"
    lower = text.lower()
    if "request rejected" in lower or "<html" in lower[:200]:
        support = ""
        if "support id" in lower:
            # F5/WAF thường trả support ID để IT whitelist
            import re

            m = re.search(r"support id is:\s*([0-9]+)", text, re.I)
            if m:
                support = f" (Support ID: {m.group(1)})"
        return (
            "WAF/firewall chan Admin API Keycloak"
            f"{support}. Token OK nhung /admin/realms bi reject — "
            "can whitelist IP may OCR tren F5/WAF Keycloak prod."
        )
    return text[:500]


def _parse_json(resp: requests.Response, *, context: str) -> object:
    """Parse JSON; raise KeycloakError khi WAF trả HTML hoặc body rỗng."""
    raw_ctype = resp.headers.get("content-type") if getattr(resp, "headers", None) else None
    ctype = raw_ctype.lower() if isinstance(raw_ctype, str) else ""
    try:
        raw_text = resp.text
    except Exception:
        raw_text = ""
    text = raw_text if isinstance(raw_text, str) else ""
    head = text.lstrip().lower()[:200]
    if "html" in ctype or head.startswith("<!doctype") or head.startswith("<html"):
        raise KeycloakError(f"{context}: {_safe_body(resp)}")
    if text and not text.strip():
        raise KeycloakError(
            f"{context}: Keycloak tra body rong (HTTP {resp.status_code})."
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise KeycloakError(
            f"{context}: khong parse duoc JSON (HTTP {resp.status_code}): "
            f"{_safe_body(resp)}"
        ) from exc


def _looks_like_html_block(resp: requests.Response) -> bool:
    raw_ctype = resp.headers.get("content-type") if getattr(resp, "headers", None) else None
    ctype = raw_ctype.lower() if isinstance(raw_ctype, str) else ""
    try:
        raw_text = resp.text
    except Exception:
        raw_text = ""
    text = raw_text if isinstance(raw_text, str) else ""
    head = text.lstrip().lower()[:200]
    return "html" in ctype or head.startswith("<!doctype") or head.startswith("<html")
