"""One-off patch script for users.py overhaul."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "app" / "routers" / "users.py"
text = p.read_text(encoding="utf-8")

if "from datetime import datetime" not in text:
    text = text.replace("import logging", "import logging\nfrom datetime import datetime")

if "_date_based_password" not in text:
    marker = "\n\ndef _resolve_temp_password"
    idx = text.find(marker)
    new_funcs = '''

def _date_based_password(suffix: str = "") -> str:
    """Mat khau reset theo ngay: NgayDDMMYYYY@ (+ suffix neu trung history)."""
    base = f"Ngay{datetime.now():%d%m%Y}@"
    return f"{base}{suffix}" if suffix else base


def _reset_password_with_retry(
    client: KeycloakClient, user_id: str, temporary: bool
) -> str:
    """Reset password voi retry khi Keycloak reject do trung history."""
    for attempt in range(6):
        pwd = _date_based_password("" if attempt == 0 else str(attempt))
        try:
            client.reset_password(user_id, pwd, temporary=temporary)
            return pwd
        except KeycloakError as exc:
            msg = str(exc).lower()
            if attempt < 5 and ("password" in msg or "history" in msg or "400" in msg):
                continue
            raise
    raise KeycloakError("Khong the dat mat khau moi sau nhieu lan thu.")
'''
    text = text[:idx] + new_funcs + text[idx:]

if "_assign_client_roles" not in text:
    old = '''def _assign_client_role(
    client: KeycloakClient, user_id: str, role_name: str
) -> list[str]:
    """Gán client role nếu user chưa có. Trả actions applied."""
    if not role_name:
        return []
    role_name = normalize_role(role_name)
    if role_name not in settings.keycloak_valid_roles:
        raise KeycloakError(f"Role không hợp lệ: '{role_name}'.")

    try:
        client_uuid = _resolve_roles_client_uuid(client)
        role_repr = client.get_client_role(client_uuid, role_name)
        if not role_repr:
            raise KeycloakError(
                f"Role '{role_name}' không tồn tại trên client "
                f"'{settings.keycloak_roles_client_id}'."
            )

        existing = client.get_user_client_roles(user_id, client_uuid)
        if any(str(r.get("name", "")) == role_name for r in existing):
            return [f"role_already:{role_name}"]

        client.assign_client_roles(user_id, client_uuid, [role_repr])
        return [f"assign_role:{role_name}"]
    except KeycloakError as exc:
        # Some realms deny role-client lookup for service account; do not block
        # user creation/update if profile attributes were already saved.
        if "403" in str(exc):
            logger.warning("Skip role assignment for '%s': %s", user_id, exc)
            return [f"assign_role_skipped:{role_name}"]
        raise'''

    new = '''def _assign_client_role(
    client: KeycloakClient, user_id: str, role_name: str
) -> list[str]:
    return _assign_client_roles(client, user_id, [role_name] if role_name else [])


def _assign_client_roles(
    client: KeycloakClient, user_id: str, role_names: list[str]
) -> list[str]:
    """Gan nhieu client role. Tra actions applied."""
    applied: list[str] = []
    for role_name in role_names:
        if not role_name:
            continue
        role_name = normalize_role(role_name)
        if role_name not in settings.keycloak_valid_roles:
            raise KeycloakError(f"Role không hợp lệ: '{role_name}'.")
        try:
            client_uuid = _resolve_roles_client_uuid(client)
            role_repr = client.get_client_role(client_uuid, role_name)
            if not role_repr:
                raise KeycloakError(
                    f"Role '{role_name}' không tồn tại trên client "
                    f"'{settings.keycloak_roles_client_id}'."
                )
            existing = client.get_user_client_roles(user_id, client_uuid)
            if any(str(r.get("name", "")) == role_name for r in existing):
                applied.append(f"role_already:{role_name}")
                continue
            client.assign_client_roles(user_id, client_uuid, [role_repr])
            applied.append(f"assign_role:{role_name}")
        except KeycloakError as exc:
            if "403" in str(exc):
                logger.warning("Skip role assignment for '%s': %s", user_id, exc)
                applied.append(f"assign_role_skipped:{role_name}")
            else:
                raise
    return applied


def _user_role_names(user: KeycloakUserInput) -> list[str]:
    if user.roles:
        return list(user.roles)
    return [user.role] if user.role else []'''

    if old in text:
        text = text.replace(old, new)

text = text.replace(
    "applied.extend(_assign_client_role(client, user_id, user.role))",
    "applied.extend(_assign_client_roles(client, user_id, _user_role_names(user)))",
)

old_reset = """        if action in (OnConflictAction.RESET_PASSWORD, OnConflictAction.RESET_BOTH):
            client.reset_password(
                user_id, _resolve_temp_password(user), temporary=temporary
            )
            client.ensure_required_actions(
                user_id, [REQUIRED_ACTION_UPDATE_PASSWORD]
            )
            applied.append(\"reset_password\")"""

new_reset = """        if action in (OnConflictAction.RESET_PASSWORD, OnConflictAction.RESET_BOTH):
            _reset_password_with_retry(client, user_id, temporary=temporary)
            client.ensure_required_actions(
                user_id, [REQUIRED_ACTION_UPDATE_PASSWORD]
            )
            applied.append(\"reset_password\")"""

if "_reset_password_with_retry(client" not in text and old_reset in text:
    text = text.replace(old_reset, new_reset)

p.write_text(text, encoding="utf-8")
print("OK")
