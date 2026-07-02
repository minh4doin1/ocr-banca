"""
OCR Service — PaddleOCR + VietOCR pipeline.

This is the core OCR engine that:
1. Uses PaddleOCR PP-Structure for layout analysis & table detection
2. Uses VietOCR for high-accuracy Vietnamese text recognition
3. Combines results into structured table data
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import settings
from app.models.schemas import CellData, PageResult, TableData
from app.utils.image_utils import deskew_image, pil_to_cv2, preprocess_for_ocr

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Lazy-loaded singleton engines (heavy models — load once)
# ──────────────────────────────────────────────────────────────

_paddle_engine = None
_paddle_ocr_fallback = None
_vietocr_predictor = None
_vietocr_config = None
_vietocr_disabled = False
_force_cpu = False


def _get_paddle_device() -> str:
    """Map config flag to PaddleX device string."""
    if _force_cpu:
        return "cpu"
    return "gpu:0" if settings.paddle_use_gpu else "cpu"


def _should_fallback_to_cpu(error: Exception) -> bool:
    """Return True if exception indicates missing GPU runtime deps."""
    text = str(error).lower()
    gpu_markers = (
        "cudnn64_8.dll",
        "cudnn",
        "error code is 126",
        "preconditionnotmet",
        "dynamic library",
        "cuda",
    )
    return any(marker in text for marker in gpu_markers)


def _get_safe_runtime_kwargs() -> dict:
    """
    Runtime flags for Windows CPU stability.
    Avoid oneDNN/PIR conversion paths that can fail on some Paddle builds.
    """
    return {
        "engine": "paddle",
        "enable_mkldnn": False,
        "enable_cinn": False,
    }


def _init_with_supported_kwargs(cls, kwargs: dict):
    """Initialize class with kwargs filtered by constructor signature."""
    sig = inspect.signature(cls.__init__)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return cls(**accepted)


def _get_paddle_engine():
    """Lazily initialise PP-Structure engine (v2/v3 compatible)."""
    global _paddle_engine, _force_cpu
    if _paddle_engine is None:
        logger.info("Loading PaddleOCR PP-Structure engine …")
        try:
            from paddleocr import PPStructureV3 as PPStructureClass
            is_v3 = True
        except ImportError:
            from paddleocr import PPStructure as PPStructureClass
            is_v3 = False

        if is_v3:
            kwargs = {
                "lang": settings.paddle_lang,
                "device": _get_paddle_device(),
                **_get_safe_runtime_kwargs(),
                "use_table_recognition": True,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "use_seal_recognition": False,
                "use_formula_recognition": False,
                "use_chart_recognition": False,
                "use_region_detection": False,
            }
        else:
            kwargs = {
                "table": True,
                "ocr": True,
                "show_log": False,
                "use_gpu": settings.paddle_use_gpu,
                "lang": settings.paddle_lang,
                "layout": True,
                "structure_version": "PP-StructureV2",
            }

        try:
            _paddle_engine = _init_with_supported_kwargs(PPStructureClass, kwargs)
        except Exception as e:
            if settings.paddle_use_gpu and _should_fallback_to_cpu(e):
                logger.warning(
                    "GPU runtime unavailable (%s). Falling back to CPU for PaddleOCR.",
                    e,
                )
                _force_cpu = True
                kwargs["device"] = "cpu"
                kwargs["use_gpu"] = False
                _paddle_engine = _init_with_supported_kwargs(PPStructureClass, kwargs)
            else:
                raise
        logger.info("PaddleOCR engine loaded successfully")
    return _paddle_engine


def _get_paddle_ocr_fallback():
    """Lazily initialise PaddleOCR for full-page fallback."""
    global _paddle_ocr_fallback, _force_cpu
    if _paddle_ocr_fallback is None:
        from paddleocr import PaddleOCR

        kwargs = {
            "lang": settings.paddle_lang,
            "use_gpu": settings.paddle_use_gpu,
            "show_log": False,
            "device": _get_paddle_device(),
            **_get_safe_runtime_kwargs(),
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        }
        try:
            _paddle_ocr_fallback = _init_with_supported_kwargs(PaddleOCR, kwargs)
        except Exception as e:
            if settings.paddle_use_gpu and _should_fallback_to_cpu(e):
                logger.warning(
                    "GPU runtime unavailable for fallback OCR (%s). Switching to CPU.",
                    e,
                )
                _force_cpu = True
                kwargs["device"] = "cpu"
                kwargs["use_gpu"] = False
                _paddle_ocr_fallback = _init_with_supported_kwargs(PaddleOCR, kwargs)
            else:
                raise
    return _paddle_ocr_fallback


def _result_to_dict(result) -> dict:
    """Normalize PaddleX result object to a plain dict."""
    if isinstance(result, dict):
        return result
    if hasattr(result, "json"):
        payload = result.json
        if isinstance(payload, dict) and "res" in payload:
            return payload["res"]
        return payload if isinstance(payload, dict) else {}
    return {}


def _get_vietocr_predictor():
    """Lazily initialise the VietOCR predictor."""
    global _vietocr_predictor, _vietocr_config, _vietocr_disabled
    if _vietocr_disabled:
        return None
    if _vietocr_predictor is None:
        try:
            logger.info("Loading VietOCR model (%s) …", settings.vietocr_model)
            from vietocr.tool.config import Cfg
            from vietocr.tool.predictor import Predictor

            config = Cfg.load_config_from_name(settings.vietocr_model)
            config["cnn"]["pretrained"] = True
            config["device"] = "cuda:0" if settings.paddle_use_gpu else "cpu"
            config["predictor"]["beamsearch"] = False  # greedy is faster

            _vietocr_config = config
            _vietocr_predictor = Predictor(config)
            logger.info("VietOCR model loaded successfully")
        except Exception as e:
            _vietocr_disabled = True
            logger.warning(
                "VietOCR unavailable (%s). Falling back to PaddleOCR for cell text.",
                e,
            )
    return _vietocr_predictor


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────


def process_page(
    image_path: str | Path,
    page_number: int,
    enable_preprocessing: bool = True,
) -> PageResult:
    """
    Run OCR on a single page image.

    Pipeline:
      image → (optional) preprocess → PaddleOCR PP-Structure
        → for each table region → extract cells → VietOCR re-recognize
        → PageResult

    Args:
        image_path: Path to the page image (PNG)
        page_number: 1-based page number
        enable_preprocessing: Whether to preprocess (deskew, denoise)

    Returns:
        PageResult with extracted tables
    """
    image_path = Path(image_path)
    logger.info("Processing page %d: %s", page_number, image_path.name)

    # Load image
    img_cv2 = cv2.imread(str(image_path))
    if img_cv2 is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # Optional preprocessing for scanned documents
    if enable_preprocessing:
        img_cv2 = deskew_image(img_cv2)

    # ── Step 1: PaddleOCR PP-StructureV3 ──
    engine = _get_paddle_engine()
    if hasattr(engine, "predict"):
        predictions = engine.predict(
            img_cv2,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_seal_recognition=False,
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_region_detection=False,
        )
    else:
        # PaddleOCR 2.x PP-Structure style
        predictions = engine(img_cv2)

    tables: list[TableData] = []
    raw_texts: list[str] = []
    table_idx = 0

    if predictions and isinstance(predictions[0], dict) and "type" in predictions[0]:
        # PaddleOCR 2.x PP-Structure output
        for region in predictions:
            region_type = region.get("type", "")

            if region_type == "table":
                table_data = _extract_table(region, img_cv2, table_idx, page_number)
                if table_data is not None:
                    tables.append(table_data)
                    table_idx += 1

            elif region_type == "text":
                res = region.get("res", [])
                if isinstance(res, list):
                    for line in res:
                        if isinstance(line, dict):
                            raw_texts.append(line.get("text", ""))
                        elif isinstance(line, (list, tuple)) and len(line) >= 2:
                            raw_texts.append(str(line[1]))

    elif predictions:
        page_res = _result_to_dict(predictions[0])

        for table_res in page_res.get("table_res_list", []):
            table_dict = _result_to_dict(table_res)
            region = {
                "type": "table",
                "res": {
                    "html": table_dict.get("pred_html", ""),
                    "cell_bbox": table_dict.get(
                        "cell_box_list", table_dict.get("cell_bbox", [])
                    ),
                },
            }
            table_data = _extract_table(region, img_cv2, table_idx, page_number)
            if table_data is not None:
                tables.append(table_data)
                table_idx += 1

        for block in page_res.get("parsing_res_list", []):
            label = block.get("block_label", "")
            content = block.get("block_content", "")
            if label != "table" and content:
                raw_texts.append(str(content))

        if not raw_texts:
            ocr_res = page_res.get("overall_ocr_res", {})
            raw_texts.extend(ocr_res.get("rec_texts", []))

    # If no tables detected by PP-Structure, try full-page OCR
    if not tables:
        logger.info("No table detected on page %d, trying full-page OCR", page_number)
        table_data = _fallback_full_page_ocr(img_cv2, page_number)
        if table_data is not None:
            tables.append(table_data)

    result = PageResult(
        page_number=page_number,
        image_path=str(image_path),
        tables=tables,
        raw_text="\n".join(raw_texts),
    )

    logger.info(
        "Page %d: found %d table(s), %d raw text lines",
        page_number,
        len(tables),
        len(raw_texts),
    )
    return result


def _extract_table(
    region: dict,
    full_image: np.ndarray,
    table_idx: int,
    page_number: int,
) -> TableData | None:
    """
    Extract structured table data from a PP-Structure table region.

    PP-Structure returns table HTML. We parse it and optionally
    re-recognize each cell with VietOCR for better Vietnamese accuracy.
    """
    try:
        res = region.get("res", {})
        html_str = ""
        cell_data_list: list[CellData] = []

        if isinstance(res, dict):
            html_str = res.get("html", res.get("pred_html", ""))
            cell_bbox = res.get("cell_bbox", res.get("cell_box_list", []))

            # Parse PP-Structure cell results
            if cell_bbox:
                cell_data_list = _parse_pp_structure_cells(
                    res, full_image, table_idx
                )
            elif html_str:
                # Fallback: parse HTML to extract cell text
                cell_data_list = _parse_html_table(html_str)

        if not cell_data_list and not html_str:
            return None

        # Determine table dimensions
        max_row = max((c.row for c in cell_data_list), default=0)
        max_col = max((c.col for c in cell_data_list), default=0)

        return TableData(
            table_index=table_idx,
            num_rows=max_row + 1,
            num_cols=max_col + 1,
            cells=cell_data_list,
            html=html_str,
        )

    except Exception as e:
        logger.error(
            "Error extracting table %d on page %d: %s",
            table_idx,
            page_number,
            e,
        )
        return None


def _parse_pp_structure_cells(
    res: dict,
    full_image: np.ndarray,
    table_idx: int,
) -> list[CellData]:
    """Parse cell data from PP-Structure result and optionally re-OCR with VietOCR."""
    cells: list[CellData] = []
    cell_bbox = res.get("cell_bbox", res.get("cell_box_list", []))

    # PP-Structure may provide cell texts directly
    # or we need to crop and re-OCR each cell
    for idx, bbox in enumerate(cell_bbox):
        row = idx  # Will be refined below
        col = 0

        # bbox format: [x1, y1, x2, y2]
        if len(bbox) >= 4:
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])

            # Crop cell from image
            cell_img = full_image[y1:y2, x1:x2]

            if cell_img.size > 0:
                # Use VietOCR for better Vietnamese recognition
                text, confidence = _recognize_with_vietocr(cell_img)
            else:
                text = ""
                confidence = 0.0

            cells.append(
                CellData(
                    row=row,
                    col=col,
                    text=text,
                    confidence=confidence,
                    bbox=[x1, y1, x2, y2],
                )
            )

    # Arrange cells into grid based on spatial position
    if cells:
        cells = _arrange_cells_into_grid(cells)

    return cells


def _arrange_cells_into_grid(cells: list[CellData]) -> list[CellData]:
    """
    Arrange cells into rows and columns based on their bounding box positions.

    Cells are grouped into rows by Y-coordinate proximity,
    then sorted by X-coordinate within each row.
    """
    if not cells:
        return cells

    # Sort by Y then X position
    sorted_cells = sorted(cells, key=lambda c: (c.bbox[1] if c.bbox else 0, c.bbox[0] if c.bbox else 0))

    # Group into rows based on Y-coordinate clustering
    row_threshold = 20  # Pixels tolerance for same row
    rows: list[list[CellData]] = []
    current_row: list[CellData] = [sorted_cells[0]]
    current_y = sorted_cells[0].bbox[1] if sorted_cells[0].bbox else 0

    for cell in sorted_cells[1:]:
        cell_y = cell.bbox[1] if cell.bbox else 0
        if abs(cell_y - current_y) <= row_threshold:
            current_row.append(cell)
        else:
            rows.append(current_row)
            current_row = [cell]
            current_y = cell_y
    rows.append(current_row)

    # Assign row/col indices
    result: list[CellData] = []
    for row_idx, row in enumerate(rows):
        # Sort columns by X position
        row_sorted = sorted(row, key=lambda c: c.bbox[0] if c.bbox else 0)
        for col_idx, cell in enumerate(row_sorted):
            result.append(
                CellData(
                    row=row_idx,
                    col=col_idx,
                    text=cell.text,
                    confidence=cell.confidence,
                    bbox=cell.bbox,
                )
            )

    return result


def _recognize_with_vietocr(cell_image: np.ndarray) -> tuple[str, float]:
    """
    Recognize Vietnamese text in a cell image using VietOCR.

    Args:
        cell_image: BGR numpy array of the cropped cell

    Returns:
        Tuple of (recognized_text, confidence_score)
    """
    try:
        predictor = _get_vietocr_predictor()
        if predictor is None:
            return _recognize_with_paddle_cell(cell_image)

        # Convert to PIL (VietOCR expects PIL Image)
        pil_img = Image.fromarray(cv2.cvtColor(cell_image, cv2.COLOR_BGR2RGB))

        # Predict
        text = predictor.predict(pil_img)
        # VietOCR doesn't return confidence directly in simple mode
        # We estimate based on text length and character validity
        confidence = _estimate_confidence(text)

        return text.strip(), confidence

    except Exception as e:
        logger.warning("VietOCR recognition failed: %s", e)
        return _recognize_with_paddle_cell(cell_image)


def _recognize_with_paddle_cell(cell_image: np.ndarray) -> tuple[str, float]:
    """Fallback cell OCR using PaddleOCR engine only."""
    try:
        ocr = _get_paddle_ocr_fallback()
        if hasattr(ocr, "predict"):
            predictions = ocr.predict(cell_image)
            if not predictions:
                return "", 0.0
            ocr_res = _result_to_dict(predictions[0])
            rec_texts = ocr_res.get("rec_texts", [])
            rec_scores = ocr_res.get("rec_scores", [])
            text = " ".join(str(t).strip() for t in rec_texts if str(t).strip())
            score = float(np.mean(rec_scores)) if rec_scores else _estimate_confidence(text)
            return text, score

        result = ocr.ocr(cell_image, cls=True)
        if not result or not result[0]:
            return "", 0.0
        texts: list[str] = []
        scores: list[float] = []
        for line in result[0]:
            text_info = line[1]
            if text_info:
                texts.append(str(text_info[0]))
                if len(text_info) > 1:
                    scores.append(float(text_info[1]))
        text = " ".join(t.strip() for t in texts if t.strip())
        score = float(np.mean(scores)) if scores else _estimate_confidence(text)
        return text, score
    except Exception:
        return "", 0.0


def _estimate_confidence(text: str) -> float:
    """
    Estimate confidence score for VietOCR output.

    Heuristic-based since VietOCR greedy mode doesn't return scores.
    """
    if not text or not text.strip():
        return 0.0

    text = text.strip()

    # Check for suspicious characters (OCR artifacts)
    suspicious_chars = set("□■●○◆◇★☆▲△▼▽◀◁▶▷♦♣♠♥")
    suspicious_count = sum(1 for c in text if c in suspicious_chars)

    if suspicious_count > len(text) * 0.3:
        return 0.3

    # Short text is often less reliable
    if len(text) <= 1:
        return 0.7

    # Vietnamese characters are a good sign
    vietnamese_chars = set("àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ")
    has_vietnamese = any(c.lower() in vietnamese_chars for c in text)

    if has_vietnamese:
        return 0.92

    return 0.85


def _parse_html_table(html: str) -> list[CellData]:
    """
    Parse table HTML (from PP-Structure) into CellData list.

    PP-Structure outputs table recognition as HTML with <td> tags.
    """
    import re

    cells: list[CellData] = []

    # Find all rows
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    for row_idx, row_html in enumerate(rows):
        # Find all cells in this row
        col_idx = 0
        cell_matches = re.finditer(
            r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL
        )
        for match in cell_matches:
            cell_text = match.group(1)
            # Remove inner HTML tags
            cell_text = re.sub(r"<[^>]+>", "", cell_text).strip()

            cells.append(
                CellData(
                    row=row_idx,
                    col=col_idx,
                    text=cell_text,
                    confidence=0.9 if cell_text else 0.0,
                    bbox=[],
                )
            )
            col_idx += 1

    return cells


def _fallback_full_page_ocr(
    image: np.ndarray, page_number: int
) -> TableData | None:
    """
    Fallback: run PaddleOCR text detection on the full page
    and try to arrange results into a table grid.

    Used when PP-Structure doesn't detect any table region.
    """
    try:
        ocr = _get_paddle_ocr_fallback()
        cells: list[CellData] = []

        if hasattr(ocr, "predict"):
            predictions = ocr.predict(image)
            if not predictions:
                return None

            ocr_res = _result_to_dict(predictions[0])
            rec_texts = ocr_res.get("rec_texts", [])
            rec_scores = ocr_res.get("rec_scores", [])
            rec_polys = ocr_res.get("rec_polys", ocr_res.get("dt_polys", []))

            if not rec_texts:
                return None

            for i, text in enumerate(rec_texts):
                confidence = float(rec_scores[i]) if i < len(rec_scores) else 0.0
                poly = rec_polys[i] if i < len(rec_polys) else []

                if len(poly) >= 4:
                    xs = [p[0] for p in poly]
                    ys = [p[1] for p in poly]
                    bbox = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]
                else:
                    bbox = []

                cells.append(
                    CellData(row=0, col=0, text=text, confidence=confidence, bbox=bbox)
                )
        else:
            result = ocr.ocr(image, cls=True)
            if not result or not result[0]:
                return None

            for line in result[0]:
                bbox_points = line[0]
                text_info = line[1]

                text = text_info[0] if text_info else ""
                confidence = float(text_info[1]) if len(text_info) > 1 else 0.0

                xs = [p[0] for p in bbox_points]
                ys = [p[1] for p in bbox_points]
                bbox = [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]

                cells.append(
                    CellData(row=0, col=0, text=text, confidence=confidence, bbox=bbox)
                )

        # Try to arrange into grid
        if cells:
            cells = _arrange_cells_into_grid(cells)

            max_row = max(c.row for c in cells)
            max_col = max(c.col for c in cells)

            return TableData(
                table_index=0,
                num_rows=max_row + 1,
                num_cols=max_col + 1,
                cells=cells,
                html="",
            )

        return None

    except Exception as e:
        logger.error("Fallback OCR failed on page %d: %s", page_number, e)
        return None
