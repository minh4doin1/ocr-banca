"""Image utility functions for preprocessing before OCR."""

from __future__ import annotations

import logging

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def pil_to_cv2(pil_image: Image.Image) -> np.ndarray:
    """Convert PIL Image to OpenCV BGR numpy array."""
    rgb = np.array(pil_image)
    if len(rgb.shape) == 2:
        # Grayscale
        return cv2.cvtColor(rgb, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def cv2_to_pil(cv2_image: np.ndarray) -> Image.Image:
    """Convert OpenCV BGR numpy array to PIL Image."""
    rgb = cv2.cvtColor(cv2_image, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def preprocess_for_ocr(image: np.ndarray) -> np.ndarray:
    """
    Preprocess image to improve OCR accuracy.

    Steps:
    1. Convert to grayscale
    2. Apply adaptive thresholding for better contrast
    3. Denoise
    4. Convert back to BGR for PaddleOCR

    Args:
        image: Input BGR image as numpy array

    Returns:
        Preprocessed BGR image
    """
    # Convert to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Denoise — gentle to preserve Vietnamese diacritics
    denoised = cv2.fastNlMeansDenoising(gray, h=10)

    # Adaptive threshold for scanned documents with uneven lighting
    # Using a large block size to handle gradual brightness changes
    binary = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=31,
        C=10,
    )

    # Convert back to BGR (PaddleOCR expects 3-channel)
    result = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    return result


def _estimate_skew_from_table_lines(gray: np.ndarray) -> float | None:
    """
    Estimate skew angle from dominant horizontal ruling lines (Hough).

    Returns angle in degrees (positive = counterclockwise) or None.
    """
    h, w = gray.shape[:2]
    # Emphasize horizontal lines
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 10
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, w // 40), 1))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    lines = cv2.HoughLinesP(
        horizontal,
        rho=1,
        theta=np.pi / 180,
        threshold=max(80, w // 8),
        minLineLength=max(60, w // 5),
        maxLineGap=20,
    )
    if lines is None or len(lines) < 3:
        return None

    angles: list[float] = []
    for line in lines[:, 0]:
        x1, y1, x2, y2 = map(int, line)
        if abs(x2 - x1) < 10:
            continue
        ang = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        # Near-horizontal only
        if abs(ang) <= 15:
            angles.append(ang)
    if len(angles) < 3:
        return None
    return float(np.median(angles))


def deskew_image(image: np.ndarray, max_angle: float = 10.0) -> np.ndarray:
    """
    Correct skew in scanned documents.

    Prefer Hough angle from table horizontal lines; fall back to minAreaRect
    on ink pixels when ruling lines are weak.

    Args:
        image: Input BGR image
        max_angle: Maximum rotation angle to correct (degrees)

    Returns:
        Deskewed image
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    angle: float | None = _estimate_skew_from_table_lines(gray)

    if angle is None:
        # Fallback: minAreaRect on inverted ink
        inv = cv2.bitwise_not(gray)
        coords = np.column_stack(np.where(inv > 0))
        if len(coords) < 100:
            logger.debug("Not enough content to determine skew angle, skipping deskew")
            return image
        rect_angle = cv2.minAreaRect(coords)[-1]
        if rect_angle < -45:
            angle = -(90 + rect_angle)
        else:
            angle = -rect_angle
    else:
        # Rotate opposite to measured line tilt
        angle = -angle

    if abs(angle) > max_angle:
        logger.debug(
            "Skew angle %.2f exceeds max_angle %.2f, skipping deskew",
            angle,
            max_angle,
        )
        return image

    if abs(angle) < 0.35:
        logger.debug("Skew angle %.2f too small, skipping deskew", angle)
        return image

    logger.info("Correcting skew angle: %.2f degrees", angle)

    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(
        image,
        rotation_matrix,
        (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )

    return rotated


def enhance_table_lines(image: np.ndarray) -> np.ndarray:
    """
    Mildly strengthen table ruling lines to help grid detection.

    Keeps original pixel values for text areas; only darkens detected lines.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Detect horizontal lines
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horizontal = cv2.morphologyEx(gray, cv2.MORPH_OPEN, horizontal_kernel)

    # Detect vertical lines
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vertical = cv2.morphologyEx(gray, cv2.MORPH_OPEN, vertical_kernel)

    # Combine lines — low values = dark lines on typical scans after OPEN on gray
    # Use binary mask of strong line responses relative to local mean
    line_sum = cv2.addWeighted(horizontal, 0.5, vertical, 0.5, 0)
    _, mask = cv2.threshold(line_sum, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Invert if needed: we want line pixels
    # Morphology OPEN on gray keeps bright paper; dark lines may be low.
    # Prefer edges of morphological open difference:
    diff = cv2.absdiff(gray, cv2.morphologyEx(gray, cv2.MORPH_CLOSE, horizontal_kernel))
    diff_v = cv2.absdiff(gray, cv2.morphologyEx(gray, cv2.MORPH_CLOSE, vertical_kernel))
    line_mask = cv2.max(diff, diff_v)
    _, line_bin = cv2.threshold(line_mask, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    result = image.copy()
    # Darken line pixels slightly (preserve text elsewhere)
    result[line_bin > 0] = (result[line_bin > 0] * 0.55).astype(np.uint8)
    return result


def prepare_page_for_ocr(image: np.ndarray, *, enhance_lines: bool = True) -> np.ndarray:
    """Deskew once, optionally enhance ruling lines (for SSO grid detect)."""
    img = deskew_image(image)
    if enhance_lines:
        img = enhance_table_lines(img)
    return img
