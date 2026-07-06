"""
Banca Core client — tra cứu chi nhánh / đại lý qua REST API.

Auth: Keycloak client_credentials (realm agribank), role banca-seller.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from app.config import settings

logger = logging.getLogger(__name__)

API_ACCEPT = "application/vnd.api.v1+json"


class BancaCoreError(Exception):
    """Lỗi khi gọi banca-core API."""


class BancaCoreClient:
    """Client mỏng gọi banca-core với cache token."""

    def __init__(self) -> None:
        if not settings.banca_core_configured:
            raise BancaCoreError(
                "Banca Core chưa cấu hình (BANCA_CORE_ENABLED + KC client)."
            )
        self.base_url = settings.banca_core_url.rstrip("/")
        self.kc_base = settings.banca_core_kc_base_url.rstrip("/")
        self.kc_realm = settings.banca_core_kc_realm
        self.client_id = settings.banca_core_kc_client_id
        self.client_secret = settings.banca_core_kc_client_secret
        self.verify_ssl = settings.banca_core_verify_ssl
        self.timeout = settings.banca_core_timeout_seconds
        self._session = requests.Session()
        self._token = ""
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    def _get_token(self) -> str:
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_expiry:
                return self._token
            url = (
                f"{self.kc_base}/realms/{self.kc_realm}"
                "/protocol/openid-connect/token"
            )
            try:
                resp = self._session.post(
                    url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    verify=self.verify_ssl,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise BancaCoreError(f"Token banca-core lỗi: {exc}") from exc
            if resp.status_code != 200:
                raise BancaCoreError(
                    f"Token banca-core thất bại HTTP {resp.status_code}"
                )
            data = resp.json()
            self._token = data.get("access_token", "")
            expires = int(data.get("expires_in", 60))
            leeway = settings.banca_core_token_leeway_seconds
            self._token_expiry = now + max(expires - leeway, 5)
            return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": API_ACCEPT,
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(
                url,
                params=params,
                headers=self._headers(),
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BancaCoreError(f"GET {path} lỗi: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise BancaCoreError(
                f"GET {path} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def lookup_agent_by_email(self, email: str) -> dict | None:
        if not email.strip():
            return None
        return self._get("/api/v1/agents/email", {"email": email.strip()})

    def search_agencies(
        self, search: str = "", page: int = 1, size: int = 20
    ) -> list[dict]:
        data = self._get(
            "/api/v1/agencies",
            {"search": search, "page": page, "size": size},
        )
        if not data:
            return []
        if isinstance(data, dict):
            return data.get("data") or data.get("content") or []
        return data if isinstance(data, list) else []

    def get_agency_descendants(self, agency_id: str) -> dict | None:
        return self._get(f"/api/v1/agencies/{agency_id}/descendants")

    def search_agents(
        self,
        search: str = "",
        agency_id: str = "",
        page: int = 1,
        size: int = 20,
    ) -> list[dict]:
        params: dict[str, Any] = {"page": page, "size": size}
        if search:
            params["search"] = search
        if agency_id:
            params["agencyId"] = agency_id
        data = self._get("/api/v1/agents", params)
        if not data:
            return []
        if isinstance(data, dict):
            return data.get("data") or data.get("content") or []
        return data if isinstance(data, list) else []


def _first_agency_code(agency: dict) -> str:
    for info in agency.get("agencyInfos") or []:
        code = (info.get("agencyCode") or "").strip()
        if code:
            return code
    return ""


def _first_agent_code(agent: dict) -> str:
    for info in agent.get("agentInfoList") or []:
        code = (info.get("agentCode") or "").strip()
        if code:
            return code
    return ""


def parse_agent_enrichment(agent: dict) -> dict[str, str]:
    """Trích branch/agent code từ AgentResponseDto."""
    agency = agent.get("agency") or {}
    branch_code = (agency.get("coreBankCode") or "").strip()
    agent_code = _first_agent_code(agent)
    if not agent_code:
        agencies = agent.get("agencies") or []
        if agencies:
            last = agencies[-1]
            branch_code = branch_code or (last.get("coreBankCode") or "").strip()
    return {
        "branch_code": branch_code,
        "agent_code": agent_code,
        "branch_name": (agency.get("name") or "").strip(),
        "agency_id": (agency.get("id") or "").strip(),
    }


def parse_agency_item(agency: dict) -> dict[str, str]:
    return {
        "id": (agency.get("id") or "").strip(),
        "name": (agency.get("name") or "").strip(),
        "core_bank_code": (agency.get("coreBankCode") or "").strip(),
        "agency_code": _first_agency_code(agency),
        "status": (agency.get("status") or "").strip(),
    }


def flatten_descendants(tree: dict | None) -> list[dict]:
    """Duyệt cây descendants, trả danh sách phẳng các node."""
    if not tree:
        return []
    nodes: list[dict] = []

    def walk(node: dict) -> None:
        if not node:
            return
        nodes.append(node)
        for child in node.get("children") or node.get("descendants") or []:
            walk(child)

    root = tree.get("agency") or tree.get("root") or tree
    if isinstance(root, dict):
        walk(root)
    elif isinstance(tree, list):
        for item in tree:
            walk(item)
    else:
        walk(tree)
    return nodes


def get_client() -> BancaCoreClient | None:
    if not settings.banca_core_configured:
        return None
    try:
        return BancaCoreClient()
    except BancaCoreError:
        return None
