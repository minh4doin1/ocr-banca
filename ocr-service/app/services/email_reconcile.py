"""
IPCAS-first reconciliation for Agribank SSO email column.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.config import settings

_DOMAIN = "@agribank.com.vn"
_DOMAIN_FRAGMENTS = ("agribank", "ribank", "bank.com", "com.vn")
_UNCERTAIN_PREFIX = "[?] "


def _email_local(email: str) -> str:
    t = (email or "").strip().lower()
    if "@" in t:
        return t.split("@", 1)[0]
    return re.sub(r"[^a-z0-9._+-]", "", t)


def _is_garbage_local(local: str) -> bool:
    if not local or len(local) < 3:
        return True
    if any(frag in local for frag in _DOMAIN_FRAGMENTS):
        return True
    if not re.fullmatch(r"[a-z][a-z0-9._-]{2,24}", local):
        return True
    return False


def _derive_from_ipcas(ipcas: str) -> str:
    """Build agribank email from IPCAS seed."""
    raw = (ipcas or "").strip().lower()
    if not raw:
        return ""
    local = re.sub(r"\s+", "", raw.split("@", 1)[0])
    local = re.sub(r"[^a-z0-9._-]", "", local)
    if not local or any(frag in local for frag in _DOMAIN_FRAGMENTS):
        return ""
    if "." in local:
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,24}", local):
        return ""
    return f"{local}{_DOMAIN}"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def reconcile_agribank_email(ocr_email: str, ipcas: str) -> tuple[str, str]:
    """
    Reconcile OCR email with IPCAS username seed.

    Returns:
        (email, source) where source is one of:
        ocr, ipcas, ipcas_override, merged, empty
    """
    if (ocr_email or "").strip().startswith(_UNCERTAIN_PREFIX):
        return (ocr_email or "").strip(), "ocr"
    if not settings.ocr_sso_email_ipcas_priority:
        ocr = (ocr_email or "").strip().lower()
        if ocr and "@" not in ocr and re.search(r"[a-z0-9]", ocr):
            ocr = f"{_email_local(ocr)}{_DOMAIN}"
        return ocr, "ocr" if ocr else "empty"

    ocr = (ocr_email or "").strip().lower()
    if ocr and "@" in ocr and not ocr.endswith(_DOMAIN):
        return ocr, "ocr"
    if ocr and "@" not in ocr:
        local_try = _email_local(ocr)
        if local_try:
            ocr = f"{local_try}{_DOMAIN}"

    ipcas_email = _derive_from_ipcas(ipcas)
    ocr_local = _email_local(ocr)
    ipcas_local = _email_local(ipcas_email)

    if not ocr and ipcas_email:
        return ipcas_email, "ipcas"
    if not ipcas_email:
        return ocr if ocr.endswith(_DOMAIN) else "", "ocr" if ocr else "empty"

    if ocr_local and ipcas_local and ocr_local == ipcas_local:
        return ocr if ocr.endswith(_DOMAIN) else f"{ocr_local}{_DOMAIN}", "ocr"

    # Valid @agribank email from OCR — keep even when IPCAS differs (common in SSO forms).
    if ocr.endswith(_DOMAIN) and ocr_local and not _is_garbage_local(ocr_local):
        return ocr, "ocr"

    if _is_garbage_local(ocr_local):
        return ipcas_email, "ipcas_override"

    if ocr.endswith(_DOMAIN):
        return ocr, "ocr"
    if ocr_local:
        return f"{ocr_local}{_DOMAIN}", "merged"
    return ipcas_email, "ipcas"


def email_mismatch_with_ipcas(ocr_email: str, ipcas: str) -> bool:
    """True when reconciled email differs from raw OCR local part."""
    reconciled, source = reconcile_agribank_email(ocr_email, ipcas)
    return source == "ipcas_override" and bool(reconciled)


def email_needs_review(email: str) -> bool:
    """True when email OCR result should be manually reviewed."""
    t = (email or "").strip()
    if not t:
        return False
    if t.startswith(_UNCERTAIN_PREFIX):
        return True
    low = t.lower()
    if "@" in low and not low.endswith(_DOMAIN):
        return True
    if "@" not in low and re.search(r"[a-zA-Z]", t):
        return True
    return False
