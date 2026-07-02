"""Pydantic schemas for OCR Service API."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class JobStatus(str, enum.Enum):
    """OCR job processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingMode(str, enum.Enum):
    """OCR processing mode chosen at upload time."""

    LOCAL = "local"
    API = "api"
    AUTO = "auto"


class CellData(BaseModel):
    """Single cell in a table."""

    row: int = Field(..., description="Row index (0-based)")
    col: int = Field(..., description="Column index (0-based)")
    text: str = Field(..., description="OCR recognized text")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="OCR confidence score"
    )
    bbox: list[int] = Field(
        default_factory=list,
        description="Bounding box [x1, y1, x2, y2] on original image",
    )


class TableData(BaseModel):
    """Extracted table data from one region."""

    table_index: int = Field(..., description="Table index on page")
    num_rows: int = Field(..., description="Number of rows")
    num_cols: int = Field(..., description="Number of columns")
    cells: list[CellData] = Field(default_factory=list)
    html: str = Field(default="", description="Table as HTML (from PP-Structure)")


class PageResult(BaseModel):
    """OCR result for a single PDF page."""

    page_number: int = Field(..., description="Page number (1-based)")
    image_path: str = Field(default="", description="Path to page image")
    tables: list[TableData] = Field(default_factory=list)
    raw_text: str = Field(
        default="", description="Full page text (non-table regions)"
    )


class OcrResult(BaseModel):
    """Complete OCR result for an uploaded PDF."""

    job_id: str
    filename: str
    total_pages: int = 0
    pages: list[PageResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class JobInfo(BaseModel):
    """Job status information returned to client."""

    job_id: str
    filename: str
    processing_mode: ProcessingMode = ProcessingMode.LOCAL
    api_provider: str = ""
    status: JobStatus
    total_pages: int = 0
    progress: int = Field(0, description="Number of pages processed")
    error_message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class UploadResponse(BaseModel):
    """Response after uploading a PDF."""

    job_id: str
    filename: str
    processing_mode: ProcessingMode = ProcessingMode.LOCAL
    api_provider: str = ""
    message: str = "PDF uploaded successfully. Processing started."


class UpdateCellRequest(BaseModel):
    """Request to update a single cell value after review."""

    page_number: int
    table_index: int
    row: int
    col: int
    text: str


class UpdateResultRequest(BaseModel):
    """Request to update OCR result after user review."""

    updates: list[UpdateCellRequest]


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str
