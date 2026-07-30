"""
Action registry — post-OCR capabilities enabled per template profile.
"""

from __future__ import annotations

from typing import Iterable

# id -> {label, description}
AVAILABLE_ACTIONS: dict[str, dict[str, str]] = {
    "validate": {
        "label": "Kiểm tra dữ liệu",
        "description": "Validate trường bắt buộc và định dạng ô",
    },
    "export_excel": {
        "label": "Xuất Excel",
        "description": "Xuất kết quả ra file Excel theo header của template",
    },
    "export_docx": {
        "label": "Xuất Word",
        "description": "Xuất kết quả ra file Word theo header của template",
    },
    "enrich_banca": {
        "label": "Bổ sung Banca Core",
        "description": "Enrich mã chi nhánh / đại lý từ Banca Core",
    },
    "provision_keycloak": {
        "label": "Tạo user Keycloak",
        "description": "Map bảng → user và tạo lô trên Keycloak",
    },
}

DEFAULT_ACTIONS = ["validate", "export_excel", "export_docx"]

SSO_ACTIONS = [
    "validate",
    "export_excel",
    "export_docx",
    "enrich_banca",
    "provision_keycloak",
]


def list_available_actions() -> list[dict[str, str]]:
    return [
        {"id": action_id, "label": meta["label"], "description": meta["description"]}
        for action_id, meta in AVAILABLE_ACTIONS.items()
    ]


def normalize_actions(actions: Iterable[str] | None) -> list[str]:
    """Keep only known actions, preserve order, dedupe."""
    if not actions:
        return list(DEFAULT_ACTIONS)
    seen: set[str] = set()
    out: list[str] = []
    for raw in actions:
        key = str(raw or "").strip().lower()
        if key in AVAILABLE_ACTIONS and key not in seen:
            seen.add(key)
            out.append(key)
    return out or list(DEFAULT_ACTIONS)


def template_allows(actions: Iterable[str] | None, action_id: str) -> bool:
    return action_id in normalize_actions(actions)
