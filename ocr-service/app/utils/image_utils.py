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


def deskew_image(image: np.ndarray, max_angle: float = 10.0) -> np.ndarray:
    """
    Correct skew in scanned documents.

    Args:
        image: Input BGR image
        max_angle: Maximum rotation angle to correct (degrees)

    Returns:
        Deskewed image
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.bitwise_not(gray)

    # Find text regions to determine skew angle
    coords = np.column_stack(np.where(gray > 0))
    if len(coords) < 100:
        logger.debug("Not enough content to determine skew angle, skipping deskew")
        return image

    angle = cv2.minAreaRect(coords)[-1]

    # Normalize angle
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle

    # Only correct if within max_angle
    if abs(angle) > max_angle:
        logger.debug(
            "Skew angle %.2f exceeds max_angle %.2f, skipping deskew",
            angle,
            max_angle,
        )
        return image

    if abs(angle) < 0.5:
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
    Enhance table border lines to help with table detection.

    Args:
        image: Input BGR image

    Returns:
        Image with enhanced table lines
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Detect horizontal lines
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
    horizontal = cv2.morphologyEx(gray, cv2.MORPH_OPEN, horizontal_kernel)

    # Detect vertical lines
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
    vertical = cv2.morphologyEx(gray, cv2.MORPH_OPEN, vertical_kernel)

    # Combine lines
    table_mask = cv2.add(horizontal, vertical)

    # Strengthen the detected lines on original image
    result = image.copy()
    result[table_mask < 128] = [0, 0, 0]  # Make lines black/stronger

    return result
