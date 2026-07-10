"""
Keycloak Admin Proxy — Configuration.

Đọc cấu hình từ environment variables (hoặc .env file khi dev local).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Cấu hình proxy, load từ env."""

    # ── Server ──
    host: str = "0.0.0.0"
    port: int = 8200
    log_level: str = "INFO"

    # ── Keycloak (cluster-internal) ──
    # DNS nội bộ cluster, KHÔNG qua F5/WAF.
    keycloak_internal_url: str = (
        "http://keycloak.keycloak.svc.cluster.local:8080"
    )
    keycloak_realm: str = "agribank"
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""
    keycloak_verify_ssl: bool = False  # internal cluster thường self-signed
    keycloak_timeout_seconds: int = 30
    keycloak_token_leeway_seconds: int = 30

    # ── Proxy auth ──
    # Shared secret OCR ↔ proxy. Empty = tắt auth (CHỈ dùng cho dev local).
    proxy_api_key: str = ""

    # ── Path lách ──
    # KHÔNG dùng tên hiển nhiên (/admin, /keycloak, /kc).
    # Path này là ingress path, mọi request bắt đầu bằng prefix này mới được route tới proxy.
    proxy_path_prefix: str = "/api/v1/iam-bridge"

    # ── Audit ──
    # Ghi audit log mỗi call: source_ip, method, kc_path, status, latency_ms
    audit_log_enabled: bool = True

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()