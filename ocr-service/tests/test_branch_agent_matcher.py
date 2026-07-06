"""Tests cho fuzzy match chi nhánh / PGD."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.models.schemas import MatchStatus
from app.services.branch_agent_matcher import (
    enrich_user_row,
    fuzzy_score,
    match_branch_and_department,
    normalize_vn,
)


def test_normalize_vn_strips_prefix_and_diacritics():
    assert normalize_vn("CN Hà Nội") == "ha noi"
    assert normalize_vn("Phòng Giao dịch Đống Đa") == "dong da"


def test_fuzzy_score_exact_and_partial():
    assert fuzzy_score("Hà Nội", "ha noi") == 1.0
    assert fuzzy_score("CN Hà Nội", "Chi nhánh Hà Nội") >= 0.9


def test_match_branch_auto_when_clear_winner():
    client = MagicMock()
    client.search_agencies.return_value = [
        {"id": "1", "name": "Chi nhánh Hà Nội", "coreBankCode": "001", "agencyCode": "DL001"},
        {"id": "2", "name": "Chi nhánh Đà Nẵng", "coreBankCode": "002", "agencyCode": "DL002"},
    ]
    client.get_agency_descendants.return_value = []

    result = match_branch_and_department(client, "Hà Nội", "")
    assert result.branch_code == "001"
    assert result.match_status in (MatchStatus.AUTO, MatchStatus.SUGGEST)


def test_enrich_user_row_skips_when_codes_present():
    client = MagicMock()
    row = enrich_user_row(
        {"username": "u", "branch_code": "001", "agent_code": "DL1"},
        client=client,
    )
    client.search_agencies.assert_not_called()
    assert row["branch_code"] == "001"


def test_enrich_user_row_email_fallback():
    client = MagicMock()
    client.search_agencies.return_value = []
    client.lookup_agent_by_email.return_value = {
        "agency": {"coreBankCode": "099", "name": "CN Test"},
        "agentInfoList": [{"agentCode": "AG99"}],
    }

    row = enrich_user_row(
        {
            "username": "u",
            "email": "seller@agribank.com.vn",
            "branch_name": "Test",
        },
        client=client,
    )
    assert row.get("branch_code") == "099"
    assert row.get("agent_code") == "AG99"
    assert row.get("enrich_source") == "email"
