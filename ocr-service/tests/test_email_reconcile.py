"""Tests for IPCAS-first email reconciliation."""

from app.services.email_reconcile import (
    email_mismatch_with_ipcas,
    reconcile_agribank_email,
)


def test_ipcas_override_when_ocr_garbage():
    email, src = reconcile_agribank_email("agribank.com.vn", "LANLUONG")
    assert email == "lanluong@agribank.com.vn"
    assert src == "ipcas_override"


def test_keep_valid_agribank_even_if_ipcas_differs():
    email, src = reconcile_agribank_email(
        "luongnguyenthiphu@agribank.com.vn", "LANLUONG"
    )
    assert email == "luongnguyenthiphu@agribank.com.vn"
    assert src == "ocr"


def test_keep_ocr_when_matches_ipcas():
    email, src = reconcile_agribank_email("lanluong@agribank.com.vn", "LANLUONG")
    assert email == "lanluong@agribank.com.vn"
    assert src == "ocr"


def test_ipcas_when_ocr_empty():
    email, src = reconcile_agribank_email("", "HQPTEST")
    assert email == "hqptest@agribank.com.vn"
    assert src == "ipcas"


def test_ipcas_when_ocr_domain_fragment():
    email, src = reconcile_agribank_email("agribank.com.vn", "USER01")
    assert email == "user01@agribank.com.vn"
    assert src == "ipcas_override"


def test_email_mismatch_flag():
    assert not email_mismatch_with_ipcas("luongnguyenthiphu@agribank.com.vn", "LANLUONG")
    assert email_mismatch_with_ipcas("agribank.com.vn", "LANLUONG")
    assert not email_mismatch_with_ipcas("lanluong@agribank.com.vn", "LANLUONG")

def test_email_from_first_line_multiline():
    from app.services.ocr_service import _email_from_first_line

    email, raw, confident = _email_from_first_line(["phuong.le", "agribank.com.vn"])
    assert confident is True
    assert email == "phuong.le@agribank.com.vn"
    assert "phuong.le" in raw


def test_email_from_first_line_uncertain():
    from app.services.ocr_service import _email_from_first_line

    email, _, confident = _email_from_first_line(["xy", "agribank.com.vn"])
    assert confident is False
    assert email.startswith("[?]")


def test_email_needs_review():
    from app.services.email_reconcile import email_needs_review

    assert email_needs_review("[?] garbage")
    assert email_needs_review("notanemail")
    assert not email_needs_review("user@agribank.com.vn")

