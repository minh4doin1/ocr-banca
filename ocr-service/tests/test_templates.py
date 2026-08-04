"""Tests for template profile registry and infer-from-Excel."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from app.models.schemas import TemplateColumn, TemplateProfile, TemplateTableConfig
from app.services.action_registry import normalize_actions, template_allows
from app.services import template_service as tpl


@pytest.fixture()
def isolated_templates(tmp_path, monkeypatch):
    """Point template storage at a temp dir."""
    from app.config import settings

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    # Clear cached dirs by touching properties
    _ = settings.templates_path
    yield tmp_path


def test_builtin_sso_created(isolated_templates):
    tpl.ensure_builtin_templates()
    profile = tpl.get_template("sso-agribank")
    assert profile is not None
    assert profile.builtin is True
    assert profile.ocr.sso_enhance is True
    assert "provision_keycloak" in profile.actions
    assert len(profile.table.columns) >= 10


def test_cannot_delete_builtin(isolated_templates):
    tpl.ensure_builtin_templates()
    with pytest.raises(ValueError, match="built-in"):
        tpl.delete_template("sso-agribank")


def test_infer_from_excel_and_save(isolated_templates):
    wb = Workbook()
    ws = wb.active
    ws.append(
        [
            "STT",
            "Họ và tên",
            "Email tại Agribank",
            "User IPCAS",
            "Số CCCD",
            "SĐT",
        ]
    )
    ws.append([1, "Nguyen Van A", "a@agribank.com.vn", "QSO001", "012345678901", "0901234567"])
    sample = Path(isolated_templates) / "sample.xlsx"
    wb.save(sample)

    draft, warnings = tpl.infer_from_sample(sample, name="Form test", template_id="form-test")
    assert draft.id == "form-test"
    assert len(draft.table.columns) == 6
    fields = {c.field for c in draft.table.columns}
    assert "name" in fields
    assert "email" in fields or "ipcas_code" in fields

    rel = tpl.store_sample_file(sample.read_bytes(), "sample.xlsx", draft.id)
    draft.source_sample = rel
    saved = tpl.save_template(draft, overwrite=True)
    assert saved.id == "form-test"
    loaded = tpl.get_template("form-test")
    assert loaded is not None
    assert loaded.name == "Form test"


def test_action_registry():
    assert template_allows(["export_excel", "validate"], "export_excel")
    assert not template_allows(["export_excel"], "provision_keycloak")
    assert normalize_actions(["export_excel", "bogus", "export_excel"]) == ["export_excel"]


def test_col_maps():
    profile = TemplateProfile(
        id="x",
        name="x",
        table=TemplateTableConfig(
            columns=[
                TemplateColumn(index=0, header="A", field="name"),
                TemplateColumn(index=1, header="B", field="email"),
            ]
        ),
    )
    assert tpl.field_to_col_index_map(profile) == {"name": 0, "email": 1}
    assert tpl.col_index_to_field_map(profile) == {0: "name", 1: "email"}
