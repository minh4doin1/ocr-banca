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
import os
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# IMPORTANT (Windows host): import torch (VietOCR backend) BEFORE PaddlePaddle
# touches PATH. paddlepaddle-gpu prepends its cuDNN dirs to PATH; if that
# happens first, the CPU torch build fails to load its DLLs (WinError 127).
# Importing torch first lets both engines coexist (Paddle GPU + VietOCR CPU).
# On Linux/Colab we skip this: torch there is a CUDA build and pre-importing it
# before paddlepaddle-gpu can trigger a pybind double-registration crash.
import sys as _sys

if _sys.platform == "win32":
    try:  # pragma: no cover - best effort, VietOCR disables itself if missing
        import torch  # noqa: F401
    except Exception:  # noqa: BLE001
        pass

from app.config import settings
from app.models.schemas import CellData, PageResult, TableData
from app.services.gpu_runtime import setup_gpu_path
from app.utils.image_utils import deskew_image, pil_to_cv2, preprocess_for_ocr

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# GPU guard — hide CUDA from Paddle unless GPU is explicitly enabled.
# paddlepaddle-gpu tries to load cuDNN during inference even in "cpu"
# device mode; if cuDNN is missing this crashes the whole job. Hiding
# the GPU before Paddle is imported guarantees a stable CPU pipeline.
# ──────────────────────────────────────────────────────────────
_paddle_imported = False


def _hide_gpu_from_paddle() -> None:
    """Set CUDA_VISIBLE_DEVICES=-1 (only effective before Paddle import)."""
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    os.environ["FLAGS_use_cuda"] = "false"


if not settings.paddle_use_gpu:
    _hide_gpu_from_paddle()
else:
    setup_gpu_path()

# ──────────────────────────────────────────────────────────────
# Lazy-loaded singleton engines (heavy models — load once)
# ──────────────────────────────────────────────────────────────

_paddle_engine = None
_paddle_ocr_fallback = None
_vietocr_predictor = None
_vietocr_config = None
_vietocr_disabled = False
_force_cpu = False
_loaded_use_gpu: bool | None = None
_device_local = threading.local()


def _get_effective_use_gpu() -> bool:
    """Resolve GPU flag: thread override → global settings."""
    override = getattr(_device_local, "use_gpu", None)
    if override is not None:
        return override
    return settings.paddle_use_gpu


def configure_ocr_device(use_gpu: bool) -> None:
    """
    Configure OCR engines for the current thread/job.

    Resets lazy-loaded models when the requested device differs
    from the previously loaded configuration.
    """
    global _paddle_engine, _paddle_ocr_fallback, _vietocr_predictor
    global _vietocr_disabled, _force_cpu, _loaded_use_gpu

    # Hide GPU from Paddle before it is imported the first time when we run
    # on CPU. When GPU requested, ensure CUDA/cuDNN are on PATH first.
    if use_gpu:
        if not _paddle_imported:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            os.environ["FLAGS_use_cuda"] = "true"
            setup_gpu_path()
    elif not _paddle_imported:
        _hide_gpu_from_paddle()

    _device_local.use_gpu = use_gpu
    effective = use_gpu and not _force_cpu

    if _loaded_use_gpu is not None and _loaded_use_gpu != effective:
        logger.info(
            "Resetting OCR engines: %s → %s",
            "GPU" if _loaded_use_gpu else "CPU",
            "GPU" if effective else "CPU",
        )
        _paddle_engine = None
        _paddle_ocr_fallback = None
        _vietocr_predictor = None
        _vietocr_disabled = False

    _loaded_use_gpu = effective


def _get_paddle_device() -> str:
    """Map config flag to PaddleX device string."""
    if _force_cpu:
        return "cpu"
    return "gpu:0" if _get_effective_use_gpu() else "cpu"


def _should_fallback_to_cpu(error: Exception) -> bool:
    """Return True if exception indicates GPU runtime is unusable."""
    text = str(error).lower()
    gpu_markers = (
        "cudnn64_8.dll",
        "cudnn",
        "error code is 126",
        "preconditionnotmet",
        "memory's size is 0",
        "tensor's dimension",
        "out of bound",
        "dynamic library",
        "cuda",
        "gpu",
    )
    return any(marker in text for marker in gpu_markers)


def is_gpu_runtime_error(error: Exception) -> bool:
    """Public helper — True when an exception indicates GPU runtime is unusable."""
    return _should_fallback_to_cpu(error)


def force_cpu_mode(reason: Exception | str = "") -> None:
    """Force all subsequent OCR calls on this process to use CPU."""
    _activate_cpu_fallback(reason)
    _device_local.use_gpu = False


def probe_local_gpu() -> tuple[bool, str]:
    """
    Lightweight Paddle GPU probe (Windows/Linux local worker).

    Returns (ok, detail). Does not load OCR models.
    """
    from app.services.gpu_runtime import probe_gpu_runtime

    status = probe_gpu_runtime()
    if status.paddle_gpu_ok:
        return True, f"{status.gpu_name} — OK"
    if not status.nvidia_detected:
        return False, status.detail or "Không có GPU NVIDIA"
    return False, status.detail


def _activate_cpu_fallback(reason: Exception | str) -> None:
    """Reset lazy-loaded engines and force CPU for subsequent OCR calls."""
    global _paddle_engine, _paddle_ocr_fallback, _vietocr_predictor
    global _vietocr_disabled, _force_cpu, _loaded_use_gpu

    logger.warning("GPU OCR failed (%s). Falling back to CPU.", reason)
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    _force_cpu = True
    _paddle_engine = None
    _paddle_ocr_fallback = None
    _vietocr_predictor = None
    _vietocr_disabled = False
    _loaded_use_gpu = False
    _device_local.use_gpu = False


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
    global _paddle_engine, _force_cpu, _paddle_imported
    if _paddle_engine is None:
        logger.info("Loading PaddleOCR PP-Structure engine …")
        _paddle_imported = True
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
                "use_gpu": _get_effective_use_gpu(),
                "lang": settings.paddle_lang,
                "layout": True,
                "structure_version": "PP-StructureV2",
            }

        try:
            _paddle_engine = _init_with_supported_kwargs(PPStructureClass, kwargs)
        except Exception as e:
            if _get_effective_use_gpu() and _should_fallback_to_cpu(e):
                _activate_cpu_fallback(e)
                kwargs["device"] = "cpu"
                kwargs["use_gpu"] = False
                _paddle_engine = _init_with_supported_kwargs(PPStructureClass, kwargs)
            else:
                raise
        logger.info("PaddleOCR engine loaded successfully")
    return _paddle_engine


def _get_paddle_ocr_fallback():
    """Lazily initialise PaddleOCR for full-page fallback."""
    global _paddle_ocr_fallback, _force_cpu, _paddle_imported
    if _paddle_ocr_fallback is None:
        _paddle_imported = True
        from paddleocr import PaddleOCR

        kwargs = {
            "lang": settings.paddle_lang,
            "use_gpu": _get_effective_use_gpu(),
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
            if _get_effective_use_gpu() and _should_fallback_to_cpu(e):
                _activate_cpu_fallback(e)
                kwargs["device"] = "cpu"
                kwargs["use_gpu"] = False
                _paddle_ocr_fallback = _init_with_supported_kwargs(PaddleOCR, kwargs)
            else:
                raise
    return _paddle_ocr_fallback


def _run_pp_structure(engine, img_cv2: np.ndarray):
    """Run PP-Structure predict/infer on one page image."""
    if hasattr(engine, "predict"):
        return engine.predict(
            img_cv2,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            use_seal_recognition=False,
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_region_detection=False,
        )
    return engine(img_cv2)


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

            # Use CUDA only if both requested AND torch actually sees a GPU.
            device = "cpu"
            if _get_effective_use_gpu():
                try:
                    import torch

                    if torch.cuda.is_available():
                        device = "cuda:0"
                except Exception:  # pragma: no cover - torch missing/broken
                    device = "cpu"

            config = Cfg.load_config_from_name(settings.vietocr_model)
            config["cnn"]["pretrained"] = True
            config["device"] = device
            config["predictor"]["beamsearch"] = False  # greedy is faster

            _vietocr_config = config
            _vietocr_predictor = Predictor(config)
            logger.info("VietOCR model loaded successfully (device=%s)", device)
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
    use_gpu: bool | None = None,
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
    if use_gpu is not None:
        configure_ocr_device(use_gpu)
    logger.info("Processing page %d: %s", page_number, image_path.name)

    # Load image
    img_cv2 = cv2.imread(str(image_path))
    if img_cv2 is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # Optional preprocessing for scanned documents
    if enable_preprocessing:
        img_cv2 = deskew_image(img_cv2)

    # ── Step 1: PaddleOCR PP-StructureV3 ──
    try:
        predictions = _run_pp_structure(_get_paddle_engine(), img_cv2)
    except Exception as e:
        if _get_effective_use_gpu() and not _force_cpu and _should_fallback_to_cpu(e):
            _activate_cpu_fallback(e)
            predictions = _run_pp_structure(_get_paddle_engine(), img_cv2)
        else:
            raise

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

    Strategy (in order of preference):
      1. Reconstruct the grid from individual text lines detected inside the
         table region, recognised with VietOCR. This gives accurate Vietnamese
         diacritics AND one physical row per line (avoids PP-Structure merging
         two source rows into a single tall cell).
      2. Fall back to PP-Structure HTML text if reconstruction is not possible.
    """
    try:
        res = region.get("res", {})
        html_str = ""
        cell_bbox: list = []
        if isinstance(res, dict):
            html_str = res.get("html", res.get("pred_html", ""))
            cell_bbox = res.get("cell_bbox", res.get("cell_box_list", []))

        table_bbox = _as_xyxy_bbox(region.get("bbox"), full_image)

        # ── Preferred: line-level reconstruction (VietOCR per line) ──
        cell_data_list = _reconstruct_table_from_lines(
            full_image, table_bbox, cell_bbox, html_str
        )

        # ── Fallback: HTML text from PP-Structure ──
        if not cell_data_list:
            html_cells = _parse_html_table(html_str) if html_str else []
            if any(c.text.strip() for c in html_cells):
                if cell_bbox:
                    bbox_cells = _parse_cell_bboxes_only(res)
                    cell_data_list = _merge_html_and_bbox_cells(html_cells, bbox_cells)
                else:
                    cell_data_list = html_cells
            elif cell_bbox:
                cell_data_list = _parse_pp_structure_cells(res, full_image, table_idx)

        if not cell_data_list and not html_str:
            return None

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


def _merge_html_and_bbox_cells(
    html_cells: list[CellData],
    bbox_cells: list[CellData],
) -> list[CellData]:
    """Attach spatial bboxes to HTML-parsed cells (HTML is source of truth for text)."""
    bbox_map = {(c.row, c.col): c for c in bbox_cells}
    merged: list[CellData] = []

    for html_cell in html_cells:
        bbox_cell = bbox_map.get((html_cell.row, html_cell.col))
        bbox = bbox_cell.bbox if bbox_cell and bbox_cell.bbox else html_cell.bbox
        merged.append(
            CellData(
                row=html_cell.row,
                col=html_cell.col,
                text=html_cell.text.strip(),
                confidence=html_cell.confidence if html_cell.text.strip() else 0.0,
                bbox=bbox,
            )
        )

    return merged


# ──────────────────────────────────────────────────────────────
# Line-level table reconstruction (accurate diacritics + full rows)
# ──────────────────────────────────────────────────────────────


def _as_xyxy_bbox(bbox, full_image: np.ndarray) -> list[int]:
    """Normalise a PP-Structure region bbox to [x1, y1, x2, y2] (full image)."""
    h, w = full_image.shape[:2]
    if bbox is None:
        return [0, 0, w, h]
    flat = list(bbox)
    xs = flat[0::2]
    ys = flat[1::2]
    if not xs or not ys:
        return [0, 0, w, h]
    x1 = max(0, int(min(xs)))
    y1 = max(0, int(min(ys)))
    x2 = min(w, int(max(xs)))
    y2 = min(h, int(max(ys)))
    if x2 <= x1 or y2 <= y1:
        return [0, 0, w, h]
    return [x1, y1, x2, y2]


def _poly_to_xyxy(poly) -> tuple[float, float, float, float] | None:
    """Convert a cell polygon (4 or 8 coords, or list of points) to (x1,y1,x2,y2)."""
    flat: list[float] = []
    for item in poly:
        if isinstance(item, (list, tuple)):
            flat.extend(float(v) for v in item)
        else:
            flat.append(float(item))
    if len(flat) < 4:
        return None
    xs = flat[0::2]
    ys = flat[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _derive_column_bounds(
    cell_bbox: list, num_cols: int
) -> list[tuple[float, float]] | None:
    """
    Derive per-column [left, right] x-ranges (table-crop coordinates) from the
    PP-Structure cell polygons. Uses the top-most row of cells as the header.
    """
    boxes = []
    for poly in cell_bbox:
        xyxy = _poly_to_xyxy(poly)
        if xyxy:
            boxes.append(xyxy)
    if not boxes or num_cols <= 0:
        return None

    boxes.sort(key=lambda b: b[1])  # by top y
    heights = [b[3] - b[1] for b in boxes]
    med_h = float(np.median(heights)) if heights else 20.0
    row_tol = max(med_h * 0.6, 8.0)

    top_y = boxes[0][1]
    header = [b for b in boxes if abs(b[1] - top_y) <= row_tol]
    header.sort(key=lambda b: b[0])

    # If the header row does not expose enough columns, cluster x-centres.
    if len(header) < num_cols:
        centers = sorted((b[0] + b[2]) / 2 for b in boxes)
        return _cluster_centers_to_bounds(centers, num_cols)

    # Merge to exactly num_cols using the widest header cells if there are extras
    header = header[:num_cols]
    bounds: list[tuple[float, float]] = []
    for i, b in enumerate(header):
        left = b[0] if i == 0 else (header[i - 1][2] + b[0]) / 2
        right = b[2] if i == len(header) - 1 else (b[2] + header[i + 1][0]) / 2
        bounds.append((left, right))
    return bounds


def _cluster_centers_to_bounds(
    centers: list[float], num_cols: int
) -> list[tuple[float, float]]:
    """Split sorted x-centres into num_cols groups; return midpoint boundaries."""
    if not centers:
        return []
    lo, hi = centers[0], centers[-1]
    step = (hi - lo) / max(num_cols, 1)
    reps = [lo + step * (i + 0.5) for i in range(num_cols)]
    bounds: list[tuple[float, float]] = []
    for i, c in enumerate(reps):
        left = -1e9 if i == 0 else (reps[i - 1] + c) / 2
        right = 1e9 if i == num_cols - 1 else (c + reps[i + 1]) / 2
        bounds.append((left, right))
    return bounds


def _assign_column(cx: float, bounds: list[tuple[float, float]]) -> int:
    """Return the column index whose x-range contains cx (nearest otherwise)."""
    for i, (left, right) in enumerate(bounds):
        if left <= cx <= right:
            return i
    centers = [(l + r) / 2 for l, r in bounds]
    return int(min(range(len(centers)), key=lambda i: abs(centers[i] - cx)))


def _expand_lines_to_column_segments(
    line_boxes: list[tuple[int, int, int, int]],
    col_bounds: list[tuple[float, float]],
    min_seg_width: int = 12,
) -> list[tuple[int, int, int, int, int]]:
    """
    Split wide detection boxes that span multiple table columns.

    PaddleOCR often returns one box covering adjacent cells (e.g. CCCD + email).
    Clipping each segment to its column x-range before VietOCR keeps values in
    the correct column.
    """
    segments: list[tuple[int, int, int, int, int]] = []

    for lx1, ly1, lx2, ly2 in line_boxes:
        overlaps: list[tuple[int, int, int]] = []
        for col_idx, (left, right) in enumerate(col_bounds):
            ix1 = max(lx1, int(left))
            ix2 = min(lx2, int(right))
            if ix2 - ix1 >= min_seg_width:
                overlaps.append((col_idx, ix1, ix2))

        if not overlaps:
            col = _assign_column((lx1 + lx2) / 2, col_bounds)
            segments.append((lx1, ly1, lx2, ly2, col))
        elif len(overlaps) == 1:
            col_idx, ix1, ix2 = overlaps[0]
            segments.append((ix1, ly1, ix2, ly2, col_idx))
        else:
            for i, (col_idx, ix1, ix2) in enumerate(overlaps):
                # Slight bleed at internal edges so VietOCR does not clip the
                # first/last character at column boundaries.
                bleed = 4
                crop_x1 = max(lx1, ix1 - (bleed if i > 0 else 0))
                crop_x2 = min(lx2, ix2 + (bleed if i < len(overlaps) - 1 else 0))
                segments.append((crop_x1, ly1, crop_x2, ly2, col_idx))

    return segments


def _detect_lines_in_region(crop: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect text-line bounding boxes inside a table crop (crop coordinates)."""
    ocr = _get_paddle_ocr_fallback()
    boxes: list[tuple[int, int, int, int]] = []

    try:
        if hasattr(ocr, "predict"):
            predictions = ocr.predict(crop)
            if predictions:
                r = _result_to_dict(predictions[0])
                polys = r.get("rec_polys", r.get("dt_polys", []))
                for poly in polys:
                    xyxy = _poly_to_xyxy(poly)
                    if xyxy:
                        boxes.append(tuple(int(v) for v in xyxy))
        else:
            # Detection only (rec=False) — VietOCR handles the actual text, so
            # skipping PaddleOCR recognition here saves a full pass per page.
            rec_off = True
            try:
                result = ocr.ocr(crop, det=True, rec=False, cls=False)
            except Exception:  # noqa: BLE001 - older API without rec kw
                rec_off = False
                result = ocr.ocr(crop, cls=False)
            if result and result[0]:
                for line in result[0]:
                    # rec=False -> line is a polygon (4 points);
                    # rec=True  -> line is [polygon, (text, score)].
                    poly = line if rec_off else line[0]
                    xyxy = _poly_to_xyxy(poly)
                    if xyxy:
                        boxes.append(tuple(int(v) for v in xyxy))
    except Exception as exc:
        if _get_effective_use_gpu() and not _force_cpu and _should_fallback_to_cpu(exc):
            _activate_cpu_fallback(exc)
            return _detect_lines_in_region(crop)
        logger.warning("Line detection failed: %s", exc)
    return boxes


def _reconstruct_table_from_lines(
    full_image: np.ndarray,
    table_bbox: list[int],
    cell_bbox: list,
    html_str: str,
) -> list[CellData]:
    """
    Rebuild a table grid from individually detected text lines.

    - Columns: derived from PP-Structure header cell x-ranges.
    - Rows: anchored on the left-most column so each physical line becomes its
      own row; wrapped continuation lines merge into the nearest row.
    - Text: recognised per line with VietOCR (falls back to PaddleOCR).
    """
    import re

    tx1, ty1, tx2, ty2 = table_bbox
    crop = full_image[ty1:ty2, tx1:tx2]
    if crop.size == 0:
        return []

    # Number of columns from the widest HTML row.
    num_cols = 0
    if html_str:
        for row_html in re.findall(r"<tr>(.*?)</tr>", html_str, re.DOTALL):
            num_cols = max(num_cols, len(re.findall(r"<td", row_html)))
    if num_cols <= 1:
        return []

    col_bounds = _derive_column_bounds(cell_bbox, num_cols)
    if not col_bounds:
        return []

    line_boxes = _detect_lines_in_region(crop)
    if not line_boxes:
        return []

    segments = _expand_lines_to_column_segments(line_boxes, col_bounds)

    # Crop each column segment, then recognise them together (batched VietOCR).
    crops: list[np.ndarray] = []
    metas: list[tuple[int, int, int, int, int]] = []
    for (lx1, ly1, lx2, ly2, col) in segments:
        pad = 2
        cy1 = max(0, ly1 - pad)
        cy2 = min(crop.shape[0], ly2 + pad)
        cx1 = max(0, lx1 - pad)
        cx2 = min(crop.shape[1], lx2 + pad)
        line_img = crop[cy1:cy2, cx1:cx2]
        if line_img.size == 0:
            continue
        crops.append(line_img)
        metas.append((lx1, ly1, lx2, ly2, col))

    recognised = _recognize_lines(crops)

    lines: list[dict] = []
    for (lx1, ly1, lx2, ly2, col), (text, conf) in zip(metas, recognised):
        text = text.strip()
        if not text:
            continue
        lines.append(
            {
                "text": text,
                "conf": conf,
                "cx": (lx1 + lx2) / 2,
                "cy": (ly1 + ly2) / 2,
                "x1": lx1,
                "y1": ly1,
                "x2": lx2,
                "y2": ly2,
                "col": col,
            }
        )

    if not lines:
        return []

    row_anchors = _build_row_anchors(lines)
    if not row_anchors:
        return []

    # Assign each line to the nearest row anchor.
    grid: dict[tuple[int, int], list[dict]] = {}
    for ln in lines:
        row = min(range(len(row_anchors)), key=lambda i: abs(row_anchors[i] - ln["cy"]))
        grid.setdefault((row, ln["col"]), []).append(ln)

    cells: list[CellData] = []
    for (row, col), members in grid.items():
        members.sort(key=lambda m: (m["y1"], m["x1"]))
        text = _normalize_cell_text(" ".join(m["text"] for m in members))
        conf = float(np.mean([m["conf"] for m in members])) if members else 0.0
        gx1 = tx1 + min(m["x1"] for m in members)
        gy1 = ty1 + min(m["y1"] for m in members)
        gx2 = tx1 + max(m["x2"] for m in members)
        gy2 = ty1 + max(m["y2"] for m in members)
        cells.append(
            CellData(
                row=row,
                col=col,
                text=text,
                confidence=conf,
                bbox=[int(gx1), int(gy1), int(gx2), int(gy2)],
            )
        )

    cells = _merge_annotation_header_rows(cells)
    cells = _fix_cccd_email_columns(cells)
    cells.sort(key=lambda c: (c.row, c.col))
    return cells


def _find_cccd_email_cols(cells: list[CellData]) -> tuple[int | None, int | None]:
    """Locate CCCD and email column indices from header rows."""
    cccd_col: int | None = None
    email_col: int | None = None
    for c in cells:
        if c.row > 1:
            break
        low = c.text.lower()
        if cccd_col is None and "cccd" in low:
            cccd_col = c.col
        if email_col is None and "email" in low:
            email_col = c.col
    return cccd_col, email_col


def _split_leading_cccd(text: str) -> tuple[str, str]:
    """If text starts with a 12-digit ID followed by email content, split them."""
    import re

    t = text.strip()
    m = re.match(r"^(\d{12}[a-zA-Z]?)\s+(.+)$", t)
    if m:
        tail = m.group(2).strip()
        if "@" in tail or "agribank" in tail.lower() or re.search(r"[a-zA-Z]", tail):
            return m.group(1), tail
    return "", t


def _normalize_cccd_text(text: str) -> str:
    """Extract a 12-digit CCCD from noisy OCR output."""
    import re

    compact = re.sub(r"\s", "", text)
    m = re.search(r"\d{12}", compact)
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", compact)
    if len(digits) == 11:
        return "0" + digits
    if len(digits) >= 12:
        return digits[:12]
    return text.strip()


def _fix_cccd_email_columns(cells: list[CellData]) -> list[CellData]:
    """Move leading CCCD numbers that landed in the email column."""
    from collections import defaultdict

    if not cells:
        return cells

    cccd_col, email_col = _find_cccd_email_cols(cells)
    if cccd_col is None or email_col is None:
        return cells

    header_rows = {c.row for c in cells if c.row <= 1}
    by_row: dict[int, dict[int, CellData]] = defaultdict(dict)
    for c in cells:
        by_row[c.row][c.col] = c

    for row, cols in by_row.items():
        if row in header_rows:
            continue
        email_cell = cols.get(email_col)
        if email_cell and email_cell.text.strip():
            cccd_part, remainder = _split_leading_cccd(email_cell.text)
            if cccd_part:
                cccd_cell = cols.get(cccd_col)
                if not (cccd_cell and cccd_cell.text.strip()):
                    email_cell.text = _normalize_cell_text(remainder)
                    if cccd_cell:
                        cccd_cell.text = _normalize_cccd_text(cccd_part)
                    else:
                        cells.append(
                            CellData(
                                row=row,
                                col=cccd_col,
                                text=_normalize_cccd_text(cccd_part),
                                confidence=email_cell.confidence,
                                bbox=[],
                            )
                        )
        cccd_cell = cols.get(cccd_col)
        if cccd_cell and cccd_cell.text.strip():
            cccd_cell.text = _normalize_cccd_text(cccd_cell.text)

    return cells


def _normalize_cell_text(text: str) -> str:
    """Clean common OCR artefacts (e.g. '@' misread as Q/(J before agribank.com.vn)."""
    import re

    t = " ".join(text.split())
    # VietOCR frequently misreads '@' as one of Q ( ) J { } [ ] | before the
    # fixed Agribank e-mail domain. Restore a single '@'.
    t = re.sub(r"[\s@Qq(){}\[\]Jj|]*agribank\.com\.vn", "@agribank.com.vn", t)
    return t.strip()


def _merge_annotation_header_rows(cells: list[CellData]) -> list[CellData]:
    """
    Merge a column-number annotation row (e.g. "(1)", "(2)", …) into the header
    row above it, then renumber rows so the grid stays contiguous.
    """
    import re
    from collections import defaultdict

    if not cells:
        return cells

    rows: dict[int, dict[int, CellData]] = defaultdict(dict)
    for c in cells:
        rows[c.row][c.col] = c
    row_ids = sorted(rows)

    paren = re.compile(r"^\(\d+\)$")
    drop: set[int] = set()
    for i in range(1, len(row_ids)):
        r = row_ids[i]
        vals = [c.text.strip() for c in rows[r].values() if c.text.strip()]
        if len(vals) >= 3 and sum(1 for v in vals if paren.match(v)) >= len(vals) * 0.6:
            prev = row_ids[i - 1]
            for col, cell in rows[r].items():
                if col in rows[prev]:
                    rows[prev][col].text = (
                        f"{rows[prev][col].text} {cell.text}".strip()
                    )
                else:
                    rows[prev][col] = CellData(
                        row=prev, col=col, text=cell.text,
                        confidence=cell.confidence, bbox=cell.bbox,
                    )
            drop.add(r)

    kept = [r for r in row_ids if r not in drop]
    renum = {old: new for new, old in enumerate(kept)}
    result: list[CellData] = []
    for r in kept:
        for col, cell in rows[r].items():
            result.append(
                CellData(
                    row=renum[r], col=col, text=cell.text,
                    confidence=cell.confidence, bbox=cell.bbox,
                )
            )
    return result


def _build_row_anchors(lines: list[dict]) -> list[float]:
    """
    Compute row anchor y-centres.

    Prefers the left-most column (STT) lines — one per physical row — so paired
    source rows are never merged. Falls back to gap-based clustering of all
    line y-centres when the anchor column is too sparse.
    """
    min_col = min(ln["col"] for ln in lines)
    anchor_lines = sorted(
        (ln for ln in lines if ln["col"] == min_col), key=lambda m: m["cy"]
    )

    heights = [ln["y2"] - ln["y1"] for ln in lines]
    med_h = float(np.median(heights)) if heights else 20.0

    total_rows = _count_row_clusters(lines, med_h)
    if len(anchor_lines) >= max(3, int(total_rows * 0.6)):
        # Merge anchors that are vertically too close (same row).
        anchors: list[float] = []
        for ln in anchor_lines:
            if anchors and abs(ln["cy"] - anchors[-1]) < med_h * 0.7:
                anchors[-1] = (anchors[-1] + ln["cy"]) / 2
            else:
                anchors.append(ln["cy"])
        return anchors

    return _cluster_row_centers(lines, med_h)


def _count_row_clusters(lines: list[dict], med_h: float) -> int:
    """Estimate the number of physical rows via y-gap clustering."""
    return len(_cluster_row_centers(lines, med_h))


def _cluster_row_centers(lines: list[dict], med_h: float) -> list[float]:
    """Cluster all line y-centres into rows using a gap threshold."""
    ys = sorted(ln["cy"] for ln in lines)
    if not ys:
        return []
    gap = max(med_h * 0.8, 10.0)
    clusters: list[list[float]] = [[ys[0]]]
    for y in ys[1:]:
        if y - clusters[-1][-1] <= gap:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    return [float(np.mean(c)) for c in clusters]


def _parse_cell_bboxes_only(res: dict) -> list[CellData]:
    """Extract cell bounding boxes without per-cell OCR (text comes from HTML)."""
    cell_bbox = res.get("cell_bbox", res.get("cell_box_list", []))
    cells: list[CellData] = []
    for bbox in cell_bbox:
        if len(bbox) >= 4:
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            cells.append(
                CellData(
                    row=0,
                    col=0,
                    text="",
                    confidence=0.0,
                    bbox=[x1, y1, x2, y2],
                )
            )
    if cells:
        cells = _arrange_cells_into_grid(cells)
    return cells


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


def _recognize_lines(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    """
    Recognise many line crops at once.

    Uses VietOCR's batched predictor when available (much faster on CPU),
    falling back to per-line recognition otherwise.
    """
    if not crops:
        return []

    predictor = _get_vietocr_predictor()
    if predictor is not None and hasattr(predictor, "predict_batch"):
        try:
            pil_imgs = [
                Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for c in crops
            ]
            texts = predictor.predict_batch(pil_imgs)
            return [(t.strip(), _estimate_confidence(t)) for t in texts]
        except Exception as e:  # noqa: BLE001
            logger.warning("VietOCR batch failed (%s); using per-line.", e)

    return [_recognize_with_vietocr(c) for c in crops]


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
        return _run_paddle_cell_ocr(cell_image)
    except Exception as exc:
        if _get_effective_use_gpu() and not _force_cpu and _should_fallback_to_cpu(exc):
            _activate_cpu_fallback(exc)
            return _run_paddle_cell_ocr(cell_image)
        return "", 0.0


def _run_paddle_cell_ocr(cell_image: np.ndarray) -> tuple[str, float]:
    """Run PaddleOCR on a single cell image."""
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
