"""Unit tests for deskew / table-line preprocessing."""

from __future__ import annotations

import numpy as np

from app.utils.image_utils import (
    deskew_image,
    enhance_table_lines,
    prepare_page_for_ocr,
)


def _make_skewed_line_image(angle_deg: float = 2.0) -> np.ndarray:
    """White page with a few dark horizontal bars, then rotate."""
    import cv2

    img = np.full((400, 600, 3), 255, dtype=np.uint8)
    for y in (80, 140, 200, 260, 320):
        cv2.line(img, (40, y), (560, y), (0, 0, 0), 2)
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w // 2, h // 2), angle_deg, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderValue=(255, 255, 255))


def test_deskew_reduces_visible_tilt():
    skewed = _make_skewed_line_image(2.5)
    fixed = deskew_image(skewed, max_angle=10.0)
    assert fixed.shape == skewed.shape
    # Should not be identical (rotation applied) for a clearly tilted page
    assert not np.array_equal(fixed, skewed)


def test_deskew_skips_tiny_angle():
    img = np.full((200, 300, 3), 255, dtype=np.uint8)
    img[50:52, 20:280] = 0
    out = deskew_image(img, max_angle=10.0)
    assert out.shape == img.shape


def test_enhance_table_lines_keeps_shape():
    img = np.full((200, 300, 3), 240, dtype=np.uint8)
    img[40:42, 10:290] = 20
    img[10:190, 80:82] = 20
    out = enhance_table_lines(img)
    assert out.shape == img.shape


def test_prepare_page_for_ocr_pipeline():
    img = _make_skewed_line_image(1.5)
    out = prepare_page_for_ocr(img, enhance_lines=True)
    assert out.shape[0] == img.shape[0]
    assert out.shape[1] == img.shape[1]
