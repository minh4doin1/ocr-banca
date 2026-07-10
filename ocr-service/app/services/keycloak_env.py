"""Resolve Keycloak settings for dev/prod target environment."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class KeycloakProfile:
    """Keycloak connection for one target environment (dev|prod)."""

    env: str
    base_url: str
    realm: str
    client_id: str
    client_secret: str
    roles_client_id: str
    roles_client_uuid: str
    role_assign_client_id: str
    role_assign_client_secret: str
    verify_ssl: bool

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url.strip()
            and self.realm.strip()
            and self.client_id.strip()
            and self.client_secret.strip()
        )

    @property
    def roles_configured(self) -> bool:
        return bool(self.roles_client_id.strip())

    @property
    def role_assign_configured(self) -> bool:
        cid = self.role_assign_client_id.strip()
        if not cid or cid == self.client_id.strip():
            return False
        return bool(self.role_assign_client_secret.strip())


def normalize_target_env(raw: str | None) -> str:
    env = (raw or "dev").strip().lower()
    return env if env in ("dev", "prod") else "dev"


def resolve_keycloak_profile(env: str | None) -> KeycloakProfile:
    """Return Keycloak settings for dev or prod (falls back to dev)."""
    target = normalize_target_env(env)
    if target == "prod" and settings.keycloak_prod_configured:
        return KeycloakProfile(
            env="prod",
            base_url=settings.keycloak_prod_base_url.strip().rstrip("/"),
            realm=(
                settings.keycloak_prod_realm.strip() or settings.keycloak_realm.strip()
            ),
            client_id=settings.keycloak_prod_client_id.strip(),
            client_secret=settings.keycloak_prod_client_secret.strip(),
            roles_client_id=(
                settings.keycloak_prod_roles_client_id.strip()
                or settings.keycloak_roles_client_id.strip()
            ),
            roles_client_uuid=settings.keycloak_prod_roles_client_uuid.strip(),
            role_assign_client_id=settings.keycloak_prod_role_assign_client_id.strip(),
            role_assign_client_secret=(
                settings.keycloak_prod_role_assign_client_secret.strip()
            ),
            verify_ssl=settings.keycloak_prod_verify_ssl,
        )

    return KeycloakProfile(
        env="dev",
        base_url=settings.keycloak_base_url.strip().rstrip("/"),
        realm=settings.keycloak_realm.strip(),
        client_id=settings.keycloak_client_id.strip(),
        client_secret=settings.keycloak_client_secret.strip(),
        roles_client_id=settings.keycloak_roles_client_id.strip(),
        roles_client_uuid="",
        role_assign_client_id=settings.keycloak_role_assign_client_id.strip(),
        role_assign_client_secret=settings.keycloak_role_assign_client_secret.strip(),
        verify_ssl=settings.keycloak_verify_ssl,
    )
