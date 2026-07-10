"""
Tests for OCR Service API endpoints.

Uses FastAPI TestClient to test the API without
needing the actual OCR models loaded.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import (
    CellData,
    JobInfo,
    JobStatus,
    OcrResult,
    PageResult,
    TableData,
)

client = TestClient(app)


# ──────────────────────────────────────────────────────────────
# Health checks
# ──────────────────────────────────────────────────────────────


def test_root():
    """Root serves the OCR frontend (HTML)."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "OCR" in response.text or "Agribank" in response.text


def test_health():
    """Test detailed health check."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")


def test_ocr_environments():
    """Frontend env switcher: Keycloak profiles; api_url empty = same-origin."""
    response = client.get("/api/ocr/environments")
    assert response.status_code == 200
    data = response.json()
    assert "server_env" in data
    assert isinstance(data["profiles"], list)
    assert len(data["profiles"]) >= 1
    assert data["profiles"][0]["id"] == "dev"
    assert data["profiles"][0]["api_url"] == ""


# ──────────────────────────────────────────────────────────────
# Upload
# ──────────────────────────────────────────────────────────────


def test_upload_rejects_non_pdf():
    """Should reject non-PDF files."""
    fake_file = io.BytesIO(b"not a pdf")
    response = client.post(
        "/api/ocr/upload",
        files={"file": ("test.txt", fake_file, "text/plain")},
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_upload_rejects_no_filename():
    """Should reject files without a filename."""
    fake_file = io.BytesIO(b"%PDF-1.4 fake pdf content")
    response = client.post(
        "/api/ocr/upload",
        files={"file": ("", fake_file, "application/pdf")},
    )
    # FastAPI returns 422 when multipart filename is empty
    assert response.status_code == 422


@patch("app.routers.ocr.process_job")
@patch("app.routers.ocr.create_job")
def test_upload_valid_pdf(mock_create_job, mock_process_job):
    """Should accept a valid PDF file."""
    mock_create_job.return_value = JobInfo(
        job_id="test123",
        filename="test.pdf",
        status=JobStatus.PENDING,
    )

    fake_pdf = io.BytesIO(b"%PDF-1.4 fake pdf content")
    response = client.post(
        "/api/ocr/upload",
        files={"file": ("test.pdf", fake_pdf, "application/pdf")},
    )
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert data["filename"] == "test.pdf"


# ──────────────────────────────────────────────────────────────
# Job Status
# ──────────────────────────────────────────────────────────────


def test_status_not_found():
    """Should return 404 for unknown job ID."""
    response = client.get("/api/ocr/status/nonexistent")
    assert response.status_code == 404


@patch("app.routers.ocr.get_job")
def test_status_found(mock_get_job):
    """Should return job status."""
    mock_get_job.return_value = JobInfo(
        job_id="abc123",
        filename="test.pdf",
        status=JobStatus.COMPLETED,
        total_pages=3,
        progress=3,
    )
    response = client.get("/api/ocr/status/abc123")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["total_pages"] == 3


# ──────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────


@patch("app.routers.ocr.get_result")
@patch("app.routers.ocr.get_job")
def test_get_result_completed(mock_get_job, mock_get_result):
    """Should return OCR result for completed job."""
    mock_get_job.return_value = JobInfo(
        job_id="abc123",
        filename="test.pdf",
        status=JobStatus.COMPLETED,
    )
    mock_get_result.return_value = OcrResult(
        job_id="abc123",
        filename="test.pdf",
        total_pages=1,
        pages=[
            PageResult(
                page_number=1,
                tables=[
                    TableData(
                        table_index=0,
                        num_rows=2,
                        num_cols=3,
                        cells=[
                            CellData(row=0, col=0, text="STT", confidence=0.95),
                            CellData(row=0, col=1, text="Họ tên", confidence=0.92),
                            CellData(row=0, col=2, text="CCCD", confidence=0.98),
                            CellData(row=1, col=0, text="1", confidence=0.99),
                            CellData(
                                row=1,
                                col=1,
                                text="Nguyễn Văn A",
                                confidence=0.88,
                            ),
                            CellData(
                                row=1,
                                col=2,
                                text="012345678901",
                                confidence=0.95,
                            ),
                        ],
                    )
                ],
            )
        ],
    )

    response = client.get("/api/ocr/result/abc123")
    assert response.status_code == 200
    data = response.json()
    assert data["total_pages"] == 1
    assert len(data["pages"][0]["tables"]) == 1
    assert data["pages"][0]["tables"][0]["num_rows"] == 2


# ──────────────────────────────────────────────────────────────
# Jobs List
# ──────────────────────────────────────────────────────────────


@patch("app.routers.ocr.get_all_jobs")
def test_list_jobs(mock_list):
    """Should return list of jobs."""
    mock_list.return_value = [
        JobInfo(
            job_id="abc123",
            filename="test1.pdf",
            status=JobStatus.COMPLETED,
        ),
        JobInfo(
            job_id="def456",
            filename="test2.pdf",
            status=JobStatus.PROCESSING,
        ),
    ]
    response = client.get("/api/ocr/jobs")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2


# ──────────────────────────────────────────────────────────────
# Schema tests
# ──────────────────────────────────────────────────────────────


def test_cell_data_schema():
    """Test CellData model validation."""
    cell = CellData(row=0, col=1, text="Nguyễn Văn B", confidence=0.95)
    assert cell.row == 0
    assert cell.col == 1
    assert "Nguyễn" in cell.text


def test_table_data_schema():
    """Test TableData model with cells."""
    table = TableData(
        table_index=0,
        num_rows=1,
        num_cols=2,
        cells=[
            CellData(row=0, col=0, text="Col A", confidence=0.9),
            CellData(row=0, col=1, text="Col B", confidence=0.8),
        ],
    )
    assert table.num_cols == 2
    assert len(table.cells) == 2


def test_keycloak_diagnostics_endpoint(monkeypatch):
    """GET /api/users/keycloak-diagnostics trả battery test theo X-OCR-Target-Env."""
    from app.models.schemas import KeycloakDiagStep, KeycloakDiagnosticsResponse

    def _fake_diag(env: str) -> KeycloakDiagnosticsResponse:
        return KeycloakDiagnosticsResponse(
            ok=False,
            target_env=env,
            summary="test summary",
            steps=[KeycloakDiagStep(step="dns", ok=True, message="ok")],
        )

    monkeypatch.setattr(
        "app.routers.users.run_keycloak_diagnostics", _fake_diag
    )
    response = client.get(
        "/api/users/keycloak-diagnostics",
        headers={"X-OCR-Target-Env": "prod"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["target_env"] == "prod"
    assert data["summary"] == "test summary"
    assert data["steps"][0]["step"] == "dns"
