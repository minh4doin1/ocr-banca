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
import re
import threading
from dataclasses import dataclass
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
from app.services.gpu_runtime import setup_gpu_path, gpu_inference_lock
from app.utils.image_utils import deskew_image, pil_to_cv2, preprocess_for_ocr

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# GPU guard — hide CUDA from Paddle unless GPU is explicitly enabled.
# paddlepaddle-gpu tries to load cuDNN during inference even in "cpu"
# device mode; if cuDNN is missing this crashes the whole job. Hiding
# the GPU before Paddle is imported guarantees a stable CPU pipeline.
# ──────────────────────────────────────────────────────────────
_paddle_imported = False
_pp_structure_unavailable = False
_pp_structure_disable_reason = ""


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
_vietocr_lock = threading.Lock()
_force_cpu = False
_loaded_use_gpu: bool | None = None
_device_local = threading.local()


def _get_effective_use_gpu() -> bool:
    """Resolve GPU flag: force CPU → thread override → global settings."""
    if _force_cpu:
        return False
    override = getattr(_device_local, "use_gpu", None)
    if override is not None:
        return override
    return settings.paddle_use_gpu


def _is_pp_structure_broken(error: Exception) -> bool:
    """True when PP-Structure table model cannot load/run (Paddle 2.6 Windows bug)."""
    text = str(error).lower()
    return "preconditionnotmet" in text or "operator < scale >" in text


def _disable_pp_structure(reason: Exception | str) -> None:
    """Switch pipeline to PaddleOCR full-page fallback (stable on this host)."""
    global _pp_structure_unavailable, _pp_structure_disable_reason, _paddle_engine

    _pp_structure_unavailable = True
    _pp_structure_disable_reason = str(reason)[:200]
    _paddle_engine = None
    logger.warning(
        "PP-Structure không khả dụng (%s) — dùng PaddleOCR full-page fallback.",
        _pp_structure_disable_reason,
    )


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
        "operator < scale >",
        "dense_tensor",
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
    os.environ["FLAGS_use_cuda"] = "false"
    try:
        import paddle

        paddle.device.set_device("cpu")
    except Exception:
        pass
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
    global _paddle_engine, _force_cpu, _paddle_imported, _pp_structure_unavailable
    if _pp_structure_unavailable:
        return None
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
            if _is_pp_structure_broken(e):
                _disable_pp_structure(e)
                return None
            if _get_effective_use_gpu() and _should_fallback_to_cpu(e):
                _activate_cpu_fallback(e)
                if is_v3:
                    kwargs["device"] = "cpu"
                kwargs["use_gpu"] = False
                try:
                    _paddle_engine = _init_with_supported_kwargs(PPStructureClass, kwargs)
                except Exception as e2:
                    if _is_pp_structure_broken(e2):
                        _disable_pp_structure(e2)
                        return None
                    raise
            else:
                raise
        if _paddle_engine is not None:
            logger.info("PaddleOCR PP-Structure engine loaded successfully")
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
    with gpu_inference_lock():
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

            # Windows: VietOCR luôn CPU (tránh xung đột CUDA với paddlepaddle-gpu).
            device = "cpu"
            if _sys.platform != "win32" and _get_effective_use_gpu():
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
            _configure_torch_threads()
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


def warmup_ocr_engines() -> None:
    """Preload Paddle + VietOCR — tránh job đầu tiên chờ 30–60s load model."""
    use_gpu = settings.paddle_use_gpu
    logger.info("Warming up OCR engines (gpu=%s)…", use_gpu)
    try:
        configure_ocr_device(use_gpu)
        engine = _get_paddle_engine()
        if engine is None:
            logger.info(
                "PP-Structure skipped (%s) — warming PaddleOCR fallback only",
                _pp_structure_disable_reason or "unavailable",
            )
        _get_paddle_ocr_fallback()
        if settings.vietocr_gpu_subprocess and settings.paddle_use_gpu:
            from app.services.vietocr_gpu_client import warmup_vietocr_gpu_worker

            if warmup_vietocr_gpu_worker():
                logger.info("VietOCR GPU subprocess warmup OK")
            else:
                logger.warning(
                    "VietOCR GPU subprocess không khởi động — dùng CPU in-process"
                )
                _get_vietocr_predictor()
        else:
            _get_vietocr_predictor()
        logger.info("OCR engine warmup complete")
    except Exception as exc:
        logger.warning("OCR warmup failed — models will load on first job: %s", exc)


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
    """
    if use_gpu is not None:
        configure_ocr_device(use_gpu)

    want_gpu = use_gpu if use_gpu is not None else _get_effective_use_gpu()
    try:
        return _process_page_impl(
            image_path, page_number, enable_preprocessing=enable_preprocessing
        )
    except Exception as exc:
        if want_gpu and not _force_cpu and _should_fallback_to_cpu(exc):
            logger.warning(
                "Page %d GPU OCR failed (%s) — retrying entire page on CPU",
                page_number,
                exc,
            )
            _activate_cpu_fallback(exc)
            configure_ocr_device(False)
            return _process_page_impl(
                image_path, page_number, enable_preprocessing=enable_preprocessing
            )
        raise


def _process_page_impl(
    image_path: str | Path,
    page_number: int,
    enable_preprocessing: bool = True,
) -> PageResult:
    """Internal page OCR (device already configured via configure_ocr_device)."""
    image_path = Path(image_path)
    logger.info("Processing page %d: %s", page_number, image_path.name)

    # Load image
    img_cv2 = cv2.imread(str(image_path))
    if img_cv2 is None:
        raise ValueError(f"Cannot read image: {image_path}")

    # Optional preprocessing for scanned documents
    if enable_preprocessing:
        img_cv2 = deskew_image(img_cv2)

    # ── Step 1: PaddleOCR PP-Structure (or full-page fallback) ──
    predictions = None
    if not _pp_structure_unavailable:
        try:
            engine = _get_paddle_engine()
            if engine is not None:
                predictions = _run_pp_structure(engine, img_cv2)
        except Exception as e:
            if _is_pp_structure_broken(e):
                _disable_pp_structure(e)
            elif _get_effective_use_gpu() and not _force_cpu and _should_fallback_to_cpu(e):
                _activate_cpu_fallback(e)
                configure_ocr_device(False)
                engine = _get_paddle_engine()
                if engine is not None:
                    predictions = _run_pp_structure(engine, img_cv2)
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
        if _pp_structure_unavailable:
            logger.info(
                "Page %d: PP-Structure off — full-page PaddleOCR pipeline",
                page_number,
            )
        else:
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

        table_kind = ""
        if (
            cell_data_list
            and settings.ocr_sso_enhance
            and _looks_like_sso_cells(cell_data_list)
        ):
            cell_data_list = _postprocess_sso_cells(cell_data_list)
            table_kind = "sso_agribank"

        max_row = max((c.row for c in cell_data_list), default=0)
        max_col = max((c.col for c in cell_data_list), default=0)

        return TableData(
            table_index=table_idx,
            num_rows=max_row + 1,
            num_cols=max_col + 1,
            cells=cell_data_list,
            html=html_str,
            table_kind=table_kind,
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
        with gpu_inference_lock():
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
    *,
    col_bounds_override: list[tuple[float, float]] | None = None,
    line_boxes_override: list[tuple[int, int, int, int]] | None = None,
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

    if col_bounds_override is not None:
        col_bounds = col_bounds_override
    else:
        num_cols = 0
        if html_str:
            for row_html in re.findall(r"<tr>(.*?)</tr>", html_str, re.DOTALL):
                num_cols = max(num_cols, len(re.findall(r"<td", row_html)))
        if num_cols <= 1:
            return []
        col_bounds = _derive_column_bounds(cell_bbox, num_cols)
        if not col_bounds:
            return []

    if line_boxes_override is not None:
        line_boxes = line_boxes_override
    else:
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

    grid = _assign_lines_to_grid(lines, row_anchors)

    max_col = max((col for (_, col) in grid.keys()), default=0)
    email_col = _resolve_sso_email_col(max_col + 1)

    cells: list[CellData] = []
    for (row, col), members in grid.items():
        members.sort(key=lambda m: (m["y1"], m["x1"]))
        text = _normalize_cell_text(
            " ".join(m["text"] for m in members),
            col=col,
            email_col=email_col,
        )
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
    if settings.ocr_sso_enhance and settings.ocr_sso_row_merge:
        cells = _merge_fragment_sso_rows(cells)
    if settings.ocr_sso_email_fixed_domain and cells:
        cells = _apply_fixed_email_domain(cells)
    if cells:
        resolved = _resolve_sso_email_col(max_col + 1, cells)
        if resolved is not None:
            header_rows = {c.row for c in cells if c.row <= 1}
            fixed = []
            for c in cells:
                if c.col == resolved and c.row not in header_rows and c.text.strip():
                    formatted = _format_sso_email(c.text)
                    if formatted:
                        fixed.append(
                            CellData(
                                row=c.row,
                                col=c.col,
                                text=formatted,
                                confidence=c.confidence,
                                bbox=c.bbox,
                            )
                        )
                    else:
                        fixed.append(c)
                else:
                    fixed.append(c)
            cells = fixed
    cells.sort(key=lambda c: (c.row, c.col))
    return cells


def _assign_lines_to_grid(
    lines: list[dict],
    row_anchors: list[float],
) -> dict[tuple[int, int], list[dict]]:
    """
    Assign detected text lines to table rows/columns.

    Wrapped lines inside one cell (e.g. email on two lines) are kept on the
    same row as the nearest STT anchor above them, not split into a new row.
    """
    if not lines or not row_anchors:
        return {}

    min_col = min(ln["col"] for ln in lines)
    heights = [ln["y2"] - ln["y1"] for ln in lines]
    med_h = float(np.median(heights)) if heights else 20.0
    wrap_gap = med_h * 2.5 if settings.ocr_sso_enhance else med_h * 1.2

    def _row_for_line(ln: dict) -> int:
        if ln["col"] == min_col:
            return min(
                range(len(row_anchors)),
                key=lambda i: abs(row_anchors[i] - ln["cy"]),
            )
        if not settings.ocr_sso_enhance:
            return min(
                range(len(row_anchors)),
                key=lambda i: abs(row_anchors[i] - ln["cy"]),
            )
        above = [
            i for i, cy in enumerate(row_anchors) if cy <= ln["cy"] + med_h * 0.4
        ]
        if above:
            anchor_idx = above[-1]
            if abs(row_anchors[anchor_idx] - ln["cy"]) <= wrap_gap:
                return anchor_idx
        return min(
            range(len(row_anchors)),
            key=lambda i: abs(row_anchors[i] - ln["cy"]),
        )

    grid: dict[tuple[int, int], list[dict]] = {}
    for ln in lines:
        row = _row_for_line(ln)
        grid.setdefault((row, ln["col"]), []).append(ln)
    return grid


def _looks_like_sso_cells(cells: list[CellData]) -> bool:
    """True when header rows match Agribank SSO table keywords."""
    texts: list[str] = []
    for c in cells:
        if c.row > 2:
            break
        texts.append(c.text)
    combined = _normalize_match_text(" ".join(texts))
    return sum(1 for kw in _SSO_HEADER_KEYWORDS if kw in combined) >= 3


def _is_email_domain_fragment(text: str) -> bool:
    import re

    t = text.strip().lower()
    if not t:
        return False
    if t in (".vn", "vn", "g"):
        return True
    return bool(
        re.search(r"ribank|agribank|\.com|@", t)
        or (len(t) <= 5 and t.endswith("ag"))
    )


def _is_cccd_date_fragment(text: str) -> bool:
    import re

    t = text.strip()
    if not t:
        return False
    if re.match(r"^\(\d", t) or re.match(r"^\d{1,2}/\d{2,4}\)?$", t):
        return True
    compact = re.sub(r"\s", "", t)
    return bool(re.match(r"^\d{5,12}$", compact))


def _is_stt_value(text: str) -> bool:
    import re

    return bool(re.match(r"^\d{1,3}$", (text or "").strip()))


def _is_vietnamese_name_fragment(text: str) -> bool:
    import re

    t = (text or "").strip()
    if not t or len(t) > 48:
        return False
    if "@" in t or re.search(r"\d{5,}", t):
        return False
    vn_chars = (
        "àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ"
    )
    if any(c in vn_chars for c in t.lower()):
        return True
    return len(t.split()) <= 4 and any(ord(c) > 127 for c in t)


def _row_looks_like_fragment_continuation(
    upper: dict[int, CellData],
    lower: dict[int, CellData],
) -> bool:
    """True when lower row is ONLY a line-wrap continuation (not a new data row)."""
    import re

    low_stt = (lower.get(0).text if lower.get(0) else "").strip()
    up_stt = (upper.get(0).text if upper.get(0) else "").strip()

    if _is_stt_value(low_stt):
        return False
    if re.search(r"\d{1,3}", low_stt):
        return False
    if not _is_stt_value(up_stt):
        return False

    lower_cols = {
        col: c.text.strip()
        for col, c in lower.items()
        if c.text.strip()
    }
    if not lower_cols:
        return False

    lower_texts = list(lower_cols.values())

    frag_hits = sum(
        1
        for t in lower_texts
        if _is_email_domain_fragment(t) or _is_cccd_date_fragment(t)
    )
    if frag_hits >= 1:
        non_frag_cols = [
            col
            for col, t in lower_cols.items()
            if not _is_email_domain_fragment(t)
            and not _is_cccd_date_fragment(t)
        ]
        if all(col in (1, 2, 7, 8) for col in non_frag_cols):
            return True
        return len(lower_cols) <= 2

    # Dòng dữ liệu đầy đủ (nhiều cột) — không gộp
    if len(lower_cols) >= 4:
        return False

    name_cols = {c for c in lower_cols if c in (1, 2)}
    other = set(lower_cols.keys()) - name_cols - {0}
    if name_cols and not other:
        return any(_is_vietnamese_name_fragment(lower_cols[c]) for c in name_cols)

    role_cols = set(lower_cols.keys()) - {0}
    if role_cols <= {7, 8} and any(
        "viên" in t.lower() or t.lower() in ("viên", "vien", "vi")
        for t in lower_texts
    ):
        return True

    return False


def _merge_row_cells_into(
    target: dict[int, CellData],
    source: dict[int, CellData],
) -> None:
    """Merge source row cells into target (in-place)."""
    for col, scell in source.items():
        stext = scell.text.strip()
        if not stext:
            continue
        if col in target:
            existing = target[col].text.strip()
            if existing:
                if (
                    stext.startswith(".")
                    or existing.endswith("@")
                    or existing.endswith("ag")
                    or (existing.endswith("(") and stext[0].isdigit())
                ):
                    target[col].text = existing + stext
                else:
                    target[col].text = f"{existing} {stext}".strip()
            else:
                target[col].text = stext
            target[col].confidence = max(target[col].confidence, scell.confidence)
            if scell.bbox:
                if target[col].bbox and len(target[col].bbox) >= 4:
                    tb, sb = target[col].bbox, scell.bbox
                    target[col].bbox = [
                        min(tb[0], sb[0]),
                        min(tb[1], sb[1]),
                        max(tb[2], sb[2]),
                        max(tb[3], sb[3]),
                    ]
                else:
                    target[col].bbox = scell.bbox
        else:
            target[col] = CellData(
                row=target[next(iter(target))].row if target else scell.row,
                col=col,
                text=stext,
                confidence=scell.confidence,
                bbox=scell.bbox,
            )


def _merge_fragment_sso_rows(
    cells: list[CellData],
    *,
    email_col: int | None = None,
) -> list[CellData]:
    """Merge OCR rows that are line-wrap continuations (email, CCCD, tên)."""
    from collections import defaultdict

    if not cells:
        return cells

    by_row: dict[int, dict[int, CellData]] = defaultdict(dict)
    for c in cells:
        by_row[c.row][c.col] = c

    merged_groups: list[dict[int, CellData]] = []
    for rid in sorted(by_row):
        cols = by_row[rid]
        if merged_groups and _row_looks_like_fragment_continuation(
            merged_groups[-1], cols
        ):
            _merge_row_cells_into(merged_groups[-1], cols)
        else:
            merged_groups.append({col: c for col, c in cols.items()})

    if email_col is None and cells:
        email_col = _resolve_sso_email_col(max(c.col for c in cells) + 1, cells)

    result: list[CellData] = []
    for new_row, group in enumerate(merged_groups):
        for col, cell in sorted(group.items()):
            result.append(
                CellData(
                    row=new_row,
                    col=col,
                    text=_normalize_cell_text(
                        cell.text, col=col, email_col=email_col
                    ),
                    confidence=cell.confidence,
                    bbox=cell.bbox,
                )
            )
    return result


def _merge_clean_row_continuations(
    clean_rows: dict[int, dict[int, CellData]],
) -> dict[int, dict[int, CellData]]:
    """Merge STT row with the row immediately below inside cleaned data rows."""
    if not clean_rows:
        return clean_rows

    row_ids = sorted(clean_rows)
    skip: set[int] = set()
    merged: dict[int, dict[int, CellData]] = {}

    for i, rid in enumerate(row_ids):
        if rid in skip:
            continue
        cols = {
            col: CellData(
                row=cell.row,
                col=cell.col,
                text=cell.text,
                confidence=cell.confidence,
                bbox=cell.bbox,
            )
            for col, cell in clean_rows[rid].items()
        }
        if i + 1 < len(row_ids):
            nxt = row_ids[i + 1]
            if nxt not in skip and _row_looks_like_fragment_continuation(
                cols, clean_rows[nxt]
            ):
                _merge_row_cells_into(cols, clean_rows[nxt])
                skip.add(nxt)
        merged[rid] = cols

    return merged


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
                    email_cell.text = (
                        _format_sso_email(remainder)
                        if settings.ocr_sso_email_fixed_domain
                        else _normalize_cell_text(
                            remainder, col=email_col, email_col=email_col
                        )
                    )
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


def _sso_email_domain() -> str:
    """Domain email SSO (luôn có prefix @)."""
    dom = (settings.ocr_sso_email_domain or "@agribank.com.vn").strip().lower()
    return dom if dom.startswith("@") else f"@{dom}"


def _resolve_sso_email_col(num_cols: int, cells: list[CellData] | None = None) -> int | None:
    """Return email column index for Agribank SSO grid."""
    if cells:
        _, email_col = _find_cccd_email_cols(cells)
        if email_col is not None:
            return email_col
    if settings.ocr_sso_email_col >= 0:
        return settings.ocr_sso_email_col
    if num_cols >= 7:
        return 5
    return None


def _find_role_col(cells: list[CellData]) -> int | None:
    """Locate role column from header rows."""
    for c in cells:
        if c.row > 1:
            break
        low = _normalize_match_text(c.text)
        if any(k in low for k in ("phan quyen", "vai tro", "role", "quyen")):
            return c.col
    max_col = max((c.col for c in cells), default=0)
    if max_col + 1 >= 8:
        return 7
    return None


def _enhance_cell_for_ocr(crop: np.ndarray, *, col_kind: str = "default") -> np.ndarray:
    """Upscale + CLAHE for small SSO cells (email/role)."""
    import cv2

    if crop is None or crop.size == 0:
        return crop
    scale = settings.ocr_sso_critical_col_upscale
    if col_kind == "role":
        scale = max(scale, 3.0)
    h, w = crop.shape[:2]
    new_w = max(int(w * scale), w + 8)
    new_h = max(int(h * scale), h + 4)
    up = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    if col_kind == "email":
        pad = 4
        up = cv2.copyMakeBorder(up, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)


def _score_email_ocr_text(text: str) -> float:
    """Higher score = more likely valid email local part."""
    import re

    t = (text or "").strip()
    if not t or _is_hallucinated_ocr_line(t):
        return 0.0
    local = _extract_sso_email_local(t)
    if not local:
        if re.search(r"[a-z]{3,}", t.lower()):
            return 0.3
        return 0.0
    if any(x in local for x in ("agribank", "ribank", "comvn")):
        return 0.1
    if len(local) < 3:
        return 0.2
    return min(1.0, 0.5 + len(local) * 0.02)


def _recognize_critical_cell(
    crop: np.ndarray,
    *,
    col_kind: str,
) -> tuple[str, float]:
    """Second-pass ensemble OCR for email/role columns."""
    enhanced = _enhance_cell_for_ocr(crop, col_kind=col_kind)
    candidates: list[tuple[str, float]] = []

    v_text, v_conf = _recognize_with_vietocr(enhanced)
    if v_text.strip():
        candidates.append((v_text.strip(), v_conf))

    p_text, p_conf = _recognize_with_paddle_cell(enhanced)
    if p_text.strip():
        candidates.append((p_text.strip(), p_conf))

    if not candidates:
        return "", 0.0

    if col_kind == "email":
        best = max(candidates, key=lambda c: _score_email_ocr_text(c[0]))
        lines = [c[0] for c in candidates if c[0]]
        if settings.ocr_sso_email_fixed_domain and lines:
            formatted, _, _ = _email_from_first_line(lines)
            return formatted or best[0], max(c[1] for c in candidates)
        lines = [c[0] for c in candidates if c[0]]
        if settings.ocr_sso_email_fixed_domain and lines:
            formatted, _, _ = _email_from_first_line(lines)
            return formatted or best[0], max(c[1] for c in candidates)
        joined = _join_multiline_ocr_lines(lines)
        return joined, max(c[1] for c in candidates)

    best = max(candidates, key=lambda c: (len(c[0]), c[1]))
    return best[0], best[1]


def _refine_sso_critical_columns(
    image: np.ndarray,
    cells: list[CellData],
) -> list[CellData]:
    """Re-OCR email and role columns with upscale (pass 2)."""
    if not settings.ocr_sso_pass2_enabled or not cells:
        return cells

    max_col = max(c.col for c in cells)
    email_col = _resolve_sso_email_col(max_col + 1, cells)
    role_col = _find_role_col(cells)
    critical = {c for c in (email_col, role_col) if c is not None}
    if not critical:
        return cells

    header_rows = {c.row for c in cells if c.row <= 1}
    out: list[CellData] = []
    for c in cells:
        if c.col not in critical or c.row in header_rows or not c.bbox or len(c.bbox) < 4:
            out.append(c)
            continue
        x1, y1, x2, y2 = [int(v) for v in c.bbox[:4]]
        pad = 2
        cy1 = max(0, y1 - pad)
        cy2 = min(image.shape[0], y2 + pad)
        cx1 = max(0, x1 - pad)
        cx2 = min(image.shape[1], x2 + pad)
        crop = image[cy1:cy2, cx1:cx2]
        if crop.size == 0 or not _cell_has_ink(crop):
            out.append(c)
            continue

        kind = "email" if c.col == email_col else "role"
        text, conf = _recognize_critical_cell(crop, col_kind=kind)
        if not text.strip():
            out.append(c)
            continue

        if kind == "email" and settings.ocr_sso_email_fixed_domain:
            text = _format_sso_email(text) or text
        elif kind == "role":
            text = text.strip()
        else:
            text = _normalize_cell_text(text, col=c.col, email_col=email_col)

        out.append(
            CellData(
                row=c.row,
                col=c.col,
                text=text,
                confidence=max(c.confidence, conf),
                bbox=c.bbox,
            )
        )
    return out


_EMAIL_LOCAL_RE = re.compile(r"^[a-z][a-z0-9._-]{2,24}$")
_EMAIL_UNCERTAIN_PREFIX = "[?] "


def _email_from_first_line(lines: list[str]) -> tuple[str, str, bool]:
    """
    Read first OCR line only up to '@', append fixed domain when confident.

    Returns (display_text, raw_joined, confident).
    """
    cleaned = [
        ln.strip()
        for ln in lines
        if ln and ln.strip() and not _is_hallucinated_ocr_line(ln.strip())
    ]
    raw = " ".join(cleaned) if cleaned else ""
    if not cleaned:
        return "", "", False

    first = cleaned[0]
    local_part = first.split("@", 1)[0] if "@" in first else first
    local = re.sub(r"\s+", "", local_part.lower())
    local = re.sub(r"[^a-z0-9._+-]", "", local)

    if _EMAIL_LOCAL_RE.fullmatch(local):
        return f"{local}{_sso_email_domain()}", raw, True

    fallback = first.strip() or raw
    if fallback and not fallback.startswith(_EMAIL_UNCERTAIN_PREFIX):
        fallback = f"{_EMAIL_UNCERTAIN_PREFIX}{fallback}"
    return fallback, raw, False


def _extract_sso_email_local(text: str) -> str:
    """Lấy phần username từ OCR (bỏ domain cố định @agribank.com.vn)."""
    import re

    t = re.sub(r"\s+", "", text.strip().lower())
    if not t:
        return ""
    t = re.sub(r"@?agribank\.com\.?vn.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"ribank\.com\.vn.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:@ag)+$", "", t, flags=re.IGNORECASE)
    t = t.rstrip("@")
    if len(t) > 4 and t.endswith("ag") and re.search(r"@ag|agribank|ribank", text, re.I):
        t = t[:-2]
    return re.sub(r"[^a-z0-9._+-]", "", t)


def _format_sso_email(text: str) -> str:
    """Ghép username OCR + domain SSO cố định (first-line khi multi-line)."""
    import re

    if not settings.ocr_sso_email_fixed_domain:
        return _repair_agribank_email(text)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) > 1:
        formatted, _, confident = _email_from_first_line(lines)
        if confident:
            return formatted
        if formatted:
            return formatted
    local = _extract_sso_email_local(text)
    if not local:
        raw = " ".join((text or "").split()).strip()
        if raw and re.search(r"[a-zA-Z0-9]", raw):
            return raw
        return ""
    return f"{local}{_sso_email_domain()}"


def _apply_fixed_email_domain(cells: list[CellData]) -> list[CellData]:
    """Gán domain cố định cho cột email (sau khi biết header)."""
    if not settings.ocr_sso_email_fixed_domain or not cells:
        return cells

    max_col = max(c.col for c in cells)
    email_col = _resolve_sso_email_col(max_col + 1, cells)
    if email_col is None:
        return cells

    header_rows = {c.row for c in cells if c.row <= 1}
    out: list[CellData] = []
    for c in cells:
        if c.col == email_col and c.row not in header_rows and c.text.strip():
            formatted = _format_sso_email(c.text)
            if formatted:
                out.append(
                    CellData(
                        row=c.row,
                        col=c.col,
                        text=formatted,
                        confidence=c.confidence,
                        bbox=c.bbox,
                    )
                )
            else:
                out.append(c)
        else:
            out.append(c)
    return out


_VN_DIACRITICS = (
    "àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ"
)


def _has_vietnamese_diacritic(text: str) -> bool:
    low = (text or "").lower()
    return any(c in _VN_DIACRITICS for c in low)


def _is_hallucinated_ocr_line(text: str) -> bool:
    """
    VietOCR hay sinh từ tiếng Anh dài trên dải ảnh gần trống (nhiễu kẻ bảng).

    Ví dụ: Concrementation, Lateralization, Incontercententalized.
    """
    import re

    t = (text or "").strip()
    if len(t) < 10:
        return False
    low = t.lower()
    if "ribank" in low or "agribank" in low or low.endswith(".vn"):
        return False
    if "@" in t or re.search(r"\d{4,}", t):
        return False
    if _has_vietnamese_diacritic(t):
        return False
    if not all(ord(c) < 128 or c.isspace() for c in t):
        return False
    letters = sum(c.isalpha() for c in t)
    return letters >= 10


def _strip_leading_english_hallucination(text: str) -> str:
    """Bỏ tiền tố tiếng Anh ảo giác trước họ tên tiếng Việt trong cùng một ô."""
    t = " ".join((text or "").split())
    if not t or not _has_vietnamese_diacritic(t):
        return t

    parts = t.split()
    first_vn = next(
        (i for i, part in enumerate(parts) if _has_vietnamese_diacritic(part)),
        None,
    )
    if first_vn is None or first_vn == 0:
        return t

    prefix = " ".join(parts[:first_vn])
    if _is_hallucinated_ocr_line(prefix) or (
        prefix.replace(" ", "").isascii() and len(prefix) >= 8
    ):
        return " ".join(parts[first_vn:]).strip()
    return t


def _join_multiline_ocr_lines(lines: list[str]) -> str:
    """Join OCR lines from one cell (email wrap, tên 2 dòng, ngày trong ngoặc)."""
    cleaned = [
        ln.strip()
        for ln in lines
        if ln and ln.strip() and not _is_hallucinated_ocr_line(ln.strip())
    ]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    out = cleaned[0]
    for nxt in cleaned[1:]:
        low_out = out.lower().rstrip()
        low_nxt = nxt.lower().lstrip()
        if (
            low_nxt.startswith("ribank")
            or low_nxt.startswith("agribank")
            or low_nxt.startswith(".vn")
            or low_out.endswith("@")
            or low_out.endswith("@ag")
            or low_out.endswith("(ag")
        ):
            out = out.rstrip() + nxt.lstrip()
        elif nxt.startswith(".") or (
            out.rstrip().endswith("(") and nxt and nxt[0].isdigit()
        ):
            out = out.rstrip() + nxt.lstrip()
        else:
            out = f"{out} {nxt}".strip()
    return out


def _repair_agribank_email(text: str) -> str:
    """Chuẩn hóa email @agribank.com.vn sau OCR multi-line / lỗi @ag lặp."""
    import re

    t = re.sub(r"\s+", "", text.strip())
    if not t:
        return text.strip()

    t = re.sub(r"ribank\.com\.vn", "agribank.com.vn", t, flags=re.IGNORECASE)
    dom = re.search(r"agribank\.com\.vn", t, flags=re.IGNORECASE)
    if not dom:
        return " ".join(text.split())

    local = t[: dom.start()]
    local = re.sub(r"(?:@ag)+$", "", local, flags=re.IGNORECASE)
    local = local.rstrip("@")
    if len(local) > 4 and local.lower().endswith("ag"):
        local = local[:-2]
    local = re.sub(r"[^a-z0-9._+-]", "", local.lower())
    if not local:
        return " ".join(text.split())
    return f"{local}@agribank.com.vn"


def _looks_like_email_content(text: str) -> bool:
    """True when OCR text is likely an email cell, not a branch/department name."""
    import re

    t = text.strip()
    if not t:
        return False
    low = t.lower()
    if "@" in t:
        return True
    if "ribank.com" in low or "agribank.com" in low:
        return True
    compact = re.sub(r"\s+", "", low)
    if "agribank" in low and re.fullmatch(r"[a-z0-9._+\-@]+", compact):
        return True
    return False


def _normalize_cell_text(
    text: str,
    *,
    col: int | None = None,
    email_col: int | None = None,
) -> str:
    """Clean common OCR artefacts (@/&/. misreads, Agribank email domain)."""
    import re

    t = " ".join(text.split())
    is_email_col = (
        email_col is not None and col is not None and col == email_col
    )
    email_like = _looks_like_email_content(t)

    if is_email_col or (email_col is None and email_like):
        if settings.ocr_sso_email_fixed_domain and (is_email_col or email_like):
            return _format_sso_email(t)
        if email_like:
            return _repair_agribank_email(t).strip()

    if settings.ocr_symbol_normalize:
        t = re.sub(r"\bKT\s*[8B&\s]+\s*NQ\b", "KT&NQ", t, flags=re.IGNORECASE)
        t = re.sub(r"\bKT8NQ\b", "KT&NQ", t, flags=re.IGNORECASE)
        t = re.sub(r"\bKT[ÁÀAáà&8\s]+NQ\b", "KT&NQ", t, flags=re.IGNORECASE)

    if settings.ocr_sso_enhance:
        t = _strip_leading_english_hallucination(t)

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

    Uses VietOCR GPU subprocess when available, else in-process batch/line.
    """
    if not crops:
        return []

    batch_size = max(1, settings.vietocr_batch_size)

    if settings.vietocr_gpu_subprocess and settings.paddle_use_gpu:
        try:
            from app.services.vietocr_gpu_client import get_vietocr_gpu_client

            gpu_client = get_vietocr_gpu_client(auto_start=True)
            if gpu_client is not None:
                results: list[tuple[str, float]] = []
                with _vietocr_lock:
                    for start in range(0, len(crops), batch_size):
                        chunk = crops[start : start + batch_size]
                        results.extend(gpu_client.predict_batch(chunk))
                return results
        except Exception as e:  # noqa: BLE001
            logger.warning("VietOCR GPU subprocess failed (%s); fallback CPU.", e)

    predictor = _get_vietocr_predictor()
    batch_size = max(1, settings.vietocr_batch_size)
    if predictor is not None and hasattr(predictor, "predict_batch"):
        try:
            pil_imgs = [
                Image.fromarray(cv2.cvtColor(c, cv2.COLOR_BGR2RGB)) for c in crops
            ]
            results: list[tuple[str, float]] = []
            with _vietocr_lock:
                for start in range(0, len(pil_imgs), batch_size):
                    chunk = pil_imgs[start : start + batch_size]
                    texts = predictor.predict_batch(chunk)
                    results.extend(
                        (t.strip(), _estimate_confidence(t)) for t in texts
                    )
            return results
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
        with _vietocr_lock:
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
    with gpu_inference_lock():
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


def _normalize_match_text(text: str) -> str:
    """Lowercase ASCII-ish form for fuzzy header matching."""
    import unicodedata

    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.replace(" ", "")


_SSO_HEADER_KEYWORDS = (
    "stt",
    "hovaten",
    "hoten",
    "phong",
    "donvi",
    "ipcas",
    "cccd",
    "email",
    "sdt",
    "phanquyen",
    "ghichu",
)


def _cluster_line_boxes_into_rows(
    boxes: list[tuple[int, int, int, int]],
    gap_ratio: float = 0.65,
) -> list[list[tuple[int, int, int, int]]]:
    """Group detection boxes into horizontal rows by y-centre proximity."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (b[1] + b[3]) / 2)
    heights = [b[3] - b[1] for b in ordered]
    med_h = float(np.median(heights)) if heights else 20.0
    gap = max(med_h * gap_ratio, 12.0)

    rows: list[list[tuple[int, int, int, int]]] = [[ordered[0]]]
    for box in ordered[1:]:
        cy = (box[1] + box[3]) / 2
        row_cy = float(np.mean([(b[1] + b[3]) / 2 for b in rows[-1]]))
        if abs(cy - row_cy) <= gap:
            rows[-1].append(box)
        else:
            rows.append([box])
    return rows


def _column_bounds_from_row_boxes(
    row: list[tuple[int, int, int, int]],
) -> list[tuple[float, float]]:
    """Derive column x-ranges from a header row's detection boxes."""
    header = sorted(row, key=lambda b: b[0])
    bounds: list[tuple[float, float]] = []
    for i, b in enumerate(header):
        left = b[0] if i == 0 else (header[i - 1][2] + b[0]) / 2
        right = b[2] if i == len(header) - 1 else (b[2] + header[i + 1][0]) / 2
        bounds.append((left, right))
    return bounds


def _crop_line_boxes(image: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> list[np.ndarray]:
    """Crop each line box from the image (with small padding)."""
    h, w = image.shape[:2]
    crops: list[np.ndarray] = []
    for x1, y1, x2, y2 in boxes:
        pad = 2
        cy1 = max(0, y1 - pad)
        cy2 = min(h, y2 + pad)
        cx1 = max(0, x1 - pad)
        cx2 = min(w, x2 + pad)
        crop = image[cy1:cy2, cx1:cx2]
        if crop.size:
            crops.append(crop)
    return crops


def _recognize_row_boxes(
    image: np.ndarray, row: list[tuple[int, int, int, int]]
) -> list[str]:
    """VietOCR each box in a row (left → right)."""
    boxes = sorted(row, key=lambda b: b[0])
    crops = _crop_line_boxes(image, boxes)
    if not crops:
        return []
    return [t for t, _ in _recognize_lines(crops)]


def _score_sso_header_row(texts: list[str]) -> int:
    """Count how many SSO table header keywords appear in a row."""
    combined = _normalize_match_text(" ".join(texts))
    return sum(1 for kw in _SSO_HEADER_KEYWORDS if kw in combined)


def _is_paren_annotation_row(texts: list[str]) -> bool:
    """True when row is the (1)(2)(3)… column-number annotation line."""
    import re

    paren = re.compile(r"^\(\d+\)$")
    vals = [t.strip() for t in texts if t.strip()]
    if len(vals) < 3:
        return False
    return sum(1 for v in vals if paren.match(v)) >= len(vals) * 0.5


def _is_section_title_row(
    row: list[tuple[int, int, int, int]], image_w: int
) -> bool:
    """Skip wide single-line section titles (e.g. 'Phòng Khách hàng…')."""
    if len(row) > 3:
        return False
    max_w = max(b[2] - b[0] for b in row)
    return max_w > image_w * 0.42


def _configure_torch_threads() -> None:
    """Limit PyTorch CPU threads for VietOCR (leave cores for Poppler + 2nd job)."""
    try:
        import torch

        n = max(1, settings.torch_num_threads)
        torch.set_num_threads(n)
        if hasattr(torch, "set_num_interop_threads"):
            torch.set_num_interop_threads(max(1, n // 2))
    except Exception:
        pass


@dataclass
class SsoGridDraft:
    """GPU/OpenCV stage output — VietOCR runs in a separate (overlapped) step."""

    page_number: int
    crop: np.ndarray
    row_lines: list[int]
    col_lines: list[int]
    table_top: int


def _cell_has_ink(crop: np.ndarray) -> bool:
    """True when cell image contains visible ink (skip empty cells before VietOCR)."""
    if crop is None or crop.size == 0:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    h, w = gray.shape
    my, mx = max(2, h // 10), max(2, w // 10)
    inner = (
        gray[my : h - my, mx : w - mx]
        if h > 2 * my and w > 2 * mx
        else gray
    )
    if inner.size == 0:
        inner = gray
    _, binary = cv2.threshold(
        inner, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    ink_ratio = float(np.count_nonzero(binary)) / max(binary.size, 1)
    if ink_ratio < settings.cell_ink_min_ratio:
        return False
    return float(np.std(inner)) >= 6.0 or ink_ratio >= settings.cell_ink_min_ratio * 2


def _merge_close_peaks(peaks: list[int], min_gap: int) -> list[int]:
    """Merge grid line peaks that are closer than min_gap pixels."""
    if not peaks:
        return []
    merged: list[int] = [peaks[0]]
    for p in peaks[1:]:
        if p - merged[-1] < min_gap:
            merged[-1] = (merged[-1] + p) // 2
        else:
            merged.append(p)
    return merged


def _filter_sliver_rows(
    row_lines: list[int], min_height: int = 18
) -> list[int]:
    """Drop grid rows whose cell height is too small (double-line artifacts)."""
    if len(row_lines) < 2:
        return row_lines
    kept = [row_lines[0]]
    for y in row_lines[1:]:
        if y - kept[-1] >= min_height:
            kept.append(y)
        elif len(row_lines) > 2:
            # merge with previous boundary
            kept[-1] = (kept[-1] + y) // 2
    return kept


def _collapse_short_row_bands(
    row_lines: list[int], ratio: float = 0.48
) -> list[int]:
    """
    Remove interior row boundaries that split one logical cell in two.

    Only collapses bands clearly shorter than half a row (~line wrap inside cell).
    """
    lines = list(row_lines)
    if len(lines) < 3:
        return lines

    for _ in range(len(lines)):
        gaps = [lines[i + 1] - lines[i] for i in range(len(lines) - 1)]
        if not gaps:
            break
        med = float(np.median(gaps))
        thr = max(14, min(med * ratio, med * 0.52))
        removed = False
        for i, gap in enumerate(gaps):
            if gap >= thr:
                continue
            if i == 0 or i >= len(gaps) - 1:
                continue
            del lines[i + 1]
            removed = True
            break
        if not removed:
            break
    return lines


def _offset_cells_bbox(
    cells: list[CellData], dy: int = 0, dx: int = 0
) -> list[CellData]:
    """Shift cell bboxes from crop-local to full-page coordinates."""
    if not dy and not dx:
        return cells
    out: list[CellData] = []
    for c in cells:
        if c.bbox and len(c.bbox) >= 4:
            x1, y1, x2, y2 = c.bbox
            bbox = [x1 + dx, y1 + dy, x2 + dx, y2 + dy]
        else:
            bbox = c.bbox
        out.append(
            CellData(
                row=c.row,
                col=c.col,
                text=c.text,
                confidence=c.confidence,
                bbox=bbox,
            )
        )
    return out


def _split_cell_text_lines(crop: np.ndarray) -> list[np.ndarray]:
    """
    Split a table cell image into horizontal text-line crops (top → bottom).

    VietOCR predict_batch on a tall multi-line cell often returns only the
    first line; OCR each sub-line then join.
    """
    if crop is None or crop.size == 0:
        return []
    h, w = crop.shape[:2]
    if h < 30 or w < 12:
        return [crop]

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    _, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    proj = np.sum(binary, axis=1).astype(np.float64)
    mx = float(np.max(proj)) if proj.size else 0.0
    if mx <= 0:
        return [crop]

    thr = max(mx * 0.06, 1.0)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, val in enumerate(proj):
        if val >= thr:
            if start is None:
                start = i
        elif start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, h))

    if len(spans) <= 1:
        return [crop]

    heights = [b - a for a, b in spans]
    med_h = float(np.median(heights)) if heights else 12.0
    merge_gap = max(4, int(med_h * 0.35))
    merged: list[tuple[int, int]] = [spans[0]]
    for y1, y2 in spans[1:]:
        py1, py2 = merged[-1]
        if y1 - py2 <= merge_gap:
            merged[-1] = (py1, y2)
        else:
            merged.append((y1, y2))

    line_crops: list[np.ndarray] = []
    pad = 1
    for y1, y2 in merged:
        if y2 - y1 < 5:
            continue
        cy1 = max(0, y1 - pad)
        cy2 = min(h, y2 + pad)
        sub = crop[cy1:cy2, :]
        if sub.size > 0 and _cell_has_ink(sub):
            line_crops.append(sub)

    return line_crops if len(line_crops) > 1 else [crop]


def _detect_grid_line_positions(
    image: np.ndarray,
    *,
    min_rows: int = 8,
    min_cols: int = 6,
    col_min_gap: int = 35,
) -> tuple[list[int], list[int]] | None:
    """
    Detect printed table grid from horizontal/vertical ruling lines.

    Works well on Agribank SSO forms (kẻ ô rõ). Returns row/col boundary y/x coords.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 4
    )
    h, w = bw.shape
    hk = max(w // 25, 25)
    vk = max(h // 40, 20)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk))
    horizontal = cv2.dilate(
        cv2.erode(bw, h_kernel, iterations=1), h_kernel, iterations=1
    )
    vertical = cv2.dilate(
        cv2.erode(bw, v_kernel, iterations=1), v_kernel, iterations=1
    )

    def _peaks(proj: np.ndarray, min_gap: int, ratio: float = 0.35) -> list[int]:
        mx = float(np.max(proj)) if proj.size else 0.0
        if mx <= 0:
            return []
        thr = mx * ratio
        peaks: list[int] = []
        for i, val in enumerate(proj):
            if val < thr:
                continue
            if not peaks or i - peaks[-1] >= min_gap:
                peaks.append(i)
            elif val > proj[peaks[-1]]:
                peaks[-1] = i
        return peaks

    row_lines = _peaks(np.sum(horizontal, axis=1), min_gap=12)
    col_lines = _peaks(np.sum(vertical, axis=0), min_gap=col_min_gap)

    if len(row_lines) >= 3:
        gaps = [row_lines[i + 1] - row_lines[i] for i in range(len(row_lines) - 1)]
        med_gap = float(np.median(gaps)) if gaps else 20.0
        row_lines = _merge_close_peaks(row_lines, max(8, int(med_gap * 0.45)))
        row_lines = _filter_sliver_rows(row_lines, min_height=max(18, int(med_gap * 0.72)))
        if settings.ocr_sso_enhance and settings.ocr_sso_collapse_row_bands:
            row_lines = _collapse_short_row_bands(row_lines)

    if len(col_lines) >= 3:
        cgaps = [col_lines[i + 1] - col_lines[i] for i in range(len(col_lines) - 1)]
        med_cgap = float(np.median(cgaps)) if cgaps else 40.0
        col_lines = _merge_close_peaks(col_lines, max(18, int(med_cgap * 0.32)))

    if len(row_lines) >= min_rows and len(col_lines) >= min_cols:
        return row_lines, col_lines
    return None


def _ocr_table_grid(
    image: np.ndarray,
    row_lines: list[int],
    col_lines: list[int],
) -> list[CellData]:
    """OCR each grid cell with VietOCR (skip empty cells to avoid hallucinations)."""
    from collections import defaultdict

    crops: list[np.ndarray] = []
    # ri, ci, x1, y1, x2, y2, line_idx_in_cell
    metas: list[tuple[int, int, int, int, int, int, int]] = []

    for ri in range(len(row_lines) - 1):
        y1, y2 = row_lines[ri], row_lines[ri + 1]
        if y2 - y1 < 14:
            continue
        for ci in range(len(col_lines) - 1):
            x1, x2 = col_lines[ci], col_lines[ci + 1]
            if x2 - x1 < 18:
                continue
            pad = 2
            cy1 = min(image.shape[0], y1 + pad)
            cy2 = max(cy1 + 1, y2 - pad)
            cx1 = min(image.shape[1], x1 + pad)
            cx2 = max(cx1 + 1, x2 - pad)
            crop = image[cy1:cy2, cx1:cx2]
            if crop.size == 0:
                continue
            if not _cell_has_ink(crop):
                continue
            if settings.ocr_sso_enhance and settings.ocr_cell_multiline:
                line_crops = _split_cell_text_lines(crop)
            else:
                line_crops = [crop]
            for li, line_crop in enumerate(line_crops):
                crops.append(line_crop)
                metas.append((ri, ci, x1, y1, x2, y2, li))

    if not crops:
        return []

    recognised = _recognize_lines(crops)
    min_conf = settings.ocr_confidence_threshold * 0.35

    grouped: dict[tuple[int, int], list[tuple[str, float, int, int, int, int, int]]] = (
        defaultdict(list)
    )
    for meta, (text, conf) in zip(metas, recognised):
        ri, ci, x1, y1, x2, y2, li = meta
        grouped[(ri, ci)].append((text, conf, li, x1, y1, x2, y2))

    cells: list[CellData] = []
    for (ri, ci), parts in grouped.items():
        parts.sort(key=lambda p: p[2])
        x1, y1, x2, y2 = parts[0][3], parts[0][4], parts[0][5], parts[0][6]
        line_texts: list[str] = []
        confs: list[float] = []
        for text, conf, _li, *_bbox in parts:
            raw = text.strip()
            if raw and not _is_hallucinated_ocr_line(raw):
                line_texts.append(raw)
                confs.append(conf)
        is_email_cell = (
            settings.ocr_sso_email_fixed_domain
            and line_texts
            and (
                (settings.ocr_sso_email_col >= 0 and ci == settings.ocr_sso_email_col)
                or any(_looks_like_email_content(ln) for ln in line_texts)
            )
        )
        if is_email_cell and len(line_texts) > 1:
            text, _, confident = _email_from_first_line(line_texts)
            text = text if (confident or text) else _join_multiline_ocr_lines(line_texts)
        else:
            text = _join_multiline_ocr_lines(line_texts)
        if not text:
            continue
        conf = float(np.mean(confs)) if confs else 0.0
        if conf < min_conf and _is_gibberish_text(text):
            continue
        cells.append(
            CellData(
                row=ri,
                col=ci,
                text=text,
                confidence=conf,
                bbox=[x1, y1, x2, y2],
            )
        )

    if not cells:
        return []

    cells = _merge_annotation_header_rows(cells)
    max_col = max(c.col for c in cells)
    email_col = _resolve_sso_email_col(max_col + 1, cells)
    header_rows = {c.row for c in cells if c.row <= 1}

    normalized: list[CellData] = []
    for c in cells:
        if c.row in header_rows:
            normalized.append(c)
            continue
        if email_col is not None and c.col == email_col:
            text = _format_sso_email(c.text) if settings.ocr_sso_email_fixed_domain else _normalize_cell_text(
                c.text, col=c.col, email_col=email_col
            )
        else:
            text = _normalize_cell_text(c.text, col=c.col, email_col=email_col)
        if not text:
            continue
        normalized.append(
            CellData(
                row=c.row,
                col=c.col,
                text=text,
                confidence=c.confidence,
                bbox=c.bbox,
            )
        )
    cells = normalized

    if settings.ocr_sso_email_fixed_domain and cells:
        cells = _apply_fixed_email_domain(cells)

    cells = _refine_sso_critical_columns(image, cells)
    return cells


def _find_table_top_y(
    image: np.ndarray, rows: list[list[tuple[int, int, int, int]]]
) -> int | None:
    """Find y-coordinate of SSO header row (STT | Họ tên | …)."""
    img_h = image.shape[0]
    best_y: int | None = None
    best_score = 0
    for row in rows:
        row_cy = int(np.mean([(b[1] + b[3]) / 2 for b in row]))
        if row_cy < img_h * 0.10 or row_cy > img_h * 0.55 or len(row) < 5:
            continue
        texts = _recognize_row_boxes(image, row)
        score = _score_sso_header_row(texts)
        if score > best_score and score >= 3:
            best_score = score
            best_y = min(b[1] for b in row)
    return best_y


def _prepare_sso_grid_draft(
    image: np.ndarray, page_number: int
) -> SsoGridDraft | None:
    """GPU detect + OpenCV grid — no VietOCR cell batch yet."""
    line_boxes = _detect_lines_in_region(image)
    if not line_boxes:
        return None

    rows = _cluster_line_boxes_into_rows(line_boxes)
    header_y = _find_table_top_y(image, rows)
    sso_header = header_y is not None
    table_top = header_y if header_y is not None else int(image.shape[0] * 0.12)

    crop = image[table_top:, :]
    if settings.ocr_sso_enhance:
        crop = deskew_image(crop)
    if settings.ocr_sso_grid_relax and sso_header:
        grid = _detect_grid_line_positions(
            crop, min_rows=6, min_cols=5, col_min_gap=28
        )
    else:
        grid = _detect_grid_line_positions(crop)
    if grid is None and settings.ocr_sso_grid_relax:
        grid = _detect_grid_line_positions(
            crop, min_rows=5, min_cols=4, col_min_gap=22
        )
    if grid is None:
        return None

    row_lines, col_lines = grid
    return SsoGridDraft(
        page_number=page_number,
        crop=crop,
        row_lines=row_lines,
        col_lines=col_lines,
        table_top=table_top,
    )


def _recognize_sso_grid_draft(draft: SsoGridDraft) -> TableData | None:
    """VietOCR batch on pre-detected grid (CPU-heavy, runs overlapped with next page GPU)."""
    cells = _ocr_table_grid(draft.crop, draft.row_lines, draft.col_lines)
    if len(cells) < 20:
        return None

    cells = _offset_cells_bbox(cells, dy=draft.table_top, dx=0)
    cells = _postprocess_sso_cells(cells)
    if not cells:
        return None

    max_row = max(c.row for c in cells)
    max_col = max(c.col for c in cells)
    logger.info(
        "Page %d: grid-line SSO table %d×%d (%d cells)",
        draft.page_number,
        max_row + 1,
        max_col + 1,
        len(cells),
    )
    return TableData(
        table_index=0,
        num_rows=max_row + 1,
        num_cols=max_col + 1,
        cells=cells,
        html="",
        table_kind="sso_agribank",
    )


def prepare_page_draft(
    image: np.ndarray,
    page_number: int,
    *,
    enable_preprocessing: bool = True,
) -> SsoGridDraft | None:
    """Public: run detect stage for pipelined page OCR."""
    img = deskew_image(image) if enable_preprocessing else image
    return _prepare_sso_grid_draft(img, page_number)


def recognize_page_draft(draft: SsoGridDraft) -> TableData | None:
    """Public: run VietOCR recognize stage for a prepared draft."""
    return _recognize_sso_grid_draft(draft)


def build_page_result_from_table(
    image_path: str | Path,
    page_number: int,
    table: TableData,
) -> PageResult:
    """Wrap TableData as PageResult."""
    return PageResult(
        page_number=page_number,
        image_path=str(image_path),
        tables=[table],
        raw_text="",
    )


def load_page_image(
    image_path: str | Path,
    *,
    enable_preprocessing: bool = True,
) -> np.ndarray:
    """Load a page PNG and optionally deskew."""
    image_path = Path(image_path)
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"Cannot read image: {image_path}")
    if enable_preprocessing:
        img = deskew_image(img)
    return img


def complete_draft_page(
    image_path: str | Path,
    draft: SsoGridDraft,
    *,
    use_gpu: bool | None = None,
) -> PageResult:
    """Run VietOCR on a prepared grid draft; fall back to full page OCR if needed."""
    table = recognize_page_draft(draft)
    if table is not None:
        return build_page_result_from_table(image_path, draft.page_number, table)
    logger.info(
        "Page %d: grid draft recognize failed — full page fallback",
        draft.page_number,
    )
    return process_page(
        image_path,
        page_number=draft.page_number,
        use_gpu=use_gpu,
    )


def _fallback_sso_grid_lines_ocr(
    image: np.ndarray, page_number: int
) -> TableData | None:
    """SSO pipeline: detect ruling lines → OCR từng ô bằng VietOCR."""
    draft = _prepare_sso_grid_draft(image, page_number)
    if draft is None:
        return None
    return _recognize_sso_grid_draft(draft)


def _find_sso_table_header(
    image: np.ndarray, rows: list[list[tuple[int, int, int, int]]]
) -> tuple[int, list[tuple[float, float]]] | tuple[None, None]:
    """Locate SSO header row and column boundaries."""
    img_h = image.shape[0]
    best_idx: int | None = None
    best_score = 0
    best_bounds: list[tuple[float, float]] = []

    for idx, row in enumerate(rows):
        row_cy = float(np.mean([(b[1] + b[3]) / 2 for b in row]))
        if row_cy < img_h * 0.12 or row_cy > img_h * 0.55:
            continue
        if len(row) < 5:
            continue
        texts = _recognize_row_boxes(image, row)
        score = _score_sso_header_row(texts)
        if score > best_score and score >= 3:
            best_score = score
            best_idx = idx
            best_bounds = _refine_sso_column_bounds(rows, idx)

    if best_idx is None:
        return None, None
    return best_idx, best_bounds


def _refine_sso_column_bounds(
    rows: list[list[tuple[int, int, int, int]]],
    header_idx: int,
    sample_rows: int = 6,
) -> list[tuple[float, float]]:
    """Derive column x-ranges from header + vài dòng dữ liệu đầu (ổn định hơn)."""
    boxes: list[tuple[int, int, int, int]] = []
    for row in rows[header_idx : header_idx + sample_rows]:
        boxes.extend(row)
    if len(boxes) < 5:
        return _column_bounds_from_row_boxes(rows[header_idx])

    header_sorted = sorted(rows[header_idx], key=lambda b: b[0])
    num_cols = max(len(header_sorted), 7)
    num_cols = min(num_cols, 10)

    centers = sorted((b[0] + b[2]) / 2 for b in boxes)
    bounds = _cluster_centers_to_bounds(centers, num_cols)

    # Căn biên trái/phải theo hàng header thực tế
    hx1 = header_sorted[0][0]
    hx2 = header_sorted[-1][2]
    if bounds:
        bounds[0] = (hx1 - 4, bounds[0][1])
        bounds[-1] = (bounds[-1][0], hx2 + 4)
    return bounds


def _is_gibberish_text(text: str) -> bool:
    """Heuristic: OCR noise on empty cells or misread grid lines."""
    import unicodedata

    t = text.strip()
    if len(t) < 8:
        return False

    # Long ASCII uppercase (CONTRACTIONALISTS, INTERMINITIC, …)
    if t.isascii():
        letters = sum(c.isalpha() for c in t)
        uppers = sum(c.isupper() for c in t)
        if letters > 8 and uppers / max(letters, 1) > 0.82:
            return True
        vowels = sum(c.lower() in "aeiou" for c in t)
        if letters > 10 and vowels / max(letters, 1) < 0.15:
            return True
        return False

    # Vietnamese-looking but no tone marks and almost no vowels → likely noise
    norm = unicodedata.normalize("NFD", t)
    has_tone = any(unicodedata.category(c) == "Mn" for c in norm)
    ascii_letters = sum(c.isascii() and c.isalpha() for c in t)
    if ascii_letters > len(t) * 0.6 and not has_tone:
        return True
    return False


def _is_valid_data_row(cols: dict[int, CellData]) -> bool:
    """Keep rows with numeric STT or enough real Vietnamese content."""
    import re

    c0 = cols.get(0)
    stt = (c0.text.strip() if c0 else "") or ""
    if re.match(r"^\d{1,3}$", stt):
        return True

    texts = [c.text.strip() for c in cols.values() if c.text.strip()]
    if not texts:
        return False
    gib = sum(1 for t in texts if _is_gibberish_text(t))
    if gib >= max(2, len(texts) // 2):
        return False
    # At least one cell with Vietnamese diacritics or email/cccd pattern
    for t in texts:
        if "@" in t or re.search(r"\d{9,12}", t):
            return True
        if any("\u0300" <= ch <= "\u036f" or ord(ch) > 127 for ch in t):
            return True
    return gib == 0 and len(texts) >= 2


_SSO_TARGET_COLS = 9


def _enforce_sso_nine_columns(cells: list[CellData]) -> list[CellData]:
    """Pad or trim SSO grid to exactly 9 columns (0..8)."""
    if not cells:
        return cells
    max_col = max(c.col for c in cells)
    if max_col + 1 == _SSO_TARGET_COLS:
        return cells
    if max_col + 1 > _SSO_TARGET_COLS:
        logger.warning(
            "SSO table has %d columns — trimming to %d",
            max_col + 1,
            _SSO_TARGET_COLS,
        )
        return [c for c in cells if c.col < _SSO_TARGET_COLS]
    logger.info(
        "SSO table has %d columns — padding to %d",
        max_col + 1,
        _SSO_TARGET_COLS,
    )
    return cells


def _postprocess_sso_cells(cells: list[CellData]) -> list[CellData]:
    """Chuẩn hóa lưới SSO: bỏ dòng rác, tách STT, sửa cột email/CCCD."""
    import re
    from collections import defaultdict

    if not cells:
        return cells

    cells = _merge_annotation_header_rows(cells)
    cells = _fix_cccd_email_columns(cells)
    email_col = _resolve_sso_email_col(
        max((c.col for c in cells), default=0) + 1, cells
    )
    if settings.ocr_sso_enhance and settings.ocr_sso_row_merge:
        cells = _merge_fragment_sso_rows(cells, email_col=email_col)
    cells = _apply_fixed_email_domain(cells)

    by_row: dict[int, dict[int, CellData]] = defaultdict(dict)
    for c in cells:
        by_row[c.row][c.col] = c

    # Bỏ dòng rác (OCR noise trên vùng kẻ bảng)
    clean_rows: dict[int, dict[int, CellData]] = {}
    for row, cols in sorted(by_row.items()):
        texts = [c.text.strip() for c in cols.values() if c.text.strip()]
        if not texts:
            continue
        if not _is_valid_data_row(cols):
            continue
        clean_rows[row] = cols

    # Tìm dòng header (STT | Họ và tên) hoặc dòng dữ liệu đầu (cột 0 = số)
    start_row = min(clean_rows) if clean_rows else 0
    for row, cols in sorted(clean_rows.items()):
        c0 = cols.get(0, CellData(row=0, col=0, text="", confidence=0)).text.strip()
        c1 = cols.get(1, CellData(row=0, col=1, text="", confidence=0)).text.strip()
        norm = _normalize_match_text(c0 + c1)
        if "stt" in norm or ("hoten" in norm and "phong" in _normalize_match_text(
            " ".join(c.text for c in cols.values())
        )):
            start_row = row
            break
        if re.match(r"^\d{1,3}$", c0) and c1 and not c1.isascii():
            start_row = row
            break

    renumbered: list[CellData] = []
    new_idx = 0
    for row in sorted(clean_rows):
        if row < start_row:
            continue
        cols = clean_rows[row]
        # Bỏ dòng header STT (chỉ giữ nếu là dòng duy nhất kiểu header)
        c0 = cols.get(0, CellData(row=0, col=0, text="", confidence=0)).text.strip()
        if "stt" in _normalize_match_text(c0):
            continue
        for col, cell in sorted(cols.items()):
            text = cell.text.strip()
            # Tách STT dính cuối tên
            m = re.search(r"^(.+?)\s+(\d{1,3})$", text)
            if m and col == 1:
                text = m.group(1).strip()
            renumbered.append(
                CellData(
                    row=new_idx,
                    col=col,
                    text=_normalize_cell_text(
                        text, col=col, email_col=email_col
                    ),
                    confidence=cell.confidence,
                    bbox=cell.bbox,
                )
            )
        new_idx += 1

    renumbered = _fix_cccd_email_columns(renumbered)
    renumbered = _enforce_sso_nine_columns(renumbered)
    renumbered.sort(key=lambda c: (c.row, c.col))
    return renumbered


def _fallback_sso_table_ocr(
    image: np.ndarray, page_number: int
) -> TableData | None:
    """
    SSO form pipeline: detect lines → find table header → VietOCR data cells.

    Excludes letterhead and section titles; keeps 7–9 column grid aligned to header.
    """
    img_h, img_w = image.shape[:2]
    line_boxes = _detect_lines_in_region(image)
    if not line_boxes:
        return None

    rows = _cluster_line_boxes_into_rows(line_boxes)
    header_idx, col_bounds_abs = _find_sso_table_header(image, rows)
    if header_idx is None or not col_bounds_abs:
        logger.info("Page %d: SSO header not found — generic fallback", page_number)
        return None

    tx1 = max(0, int(min(b[0] for b in rows[header_idx])) - 8)
    tx2 = min(img_w, int(max(b[2] for b in rows[header_idx])) + 8)
    ty1 = max(0, int(min(b[1] for b in rows[header_idx])) - 4)
    ty2 = img_h - 10

    table_boxes: list[tuple[int, int, int, int]] = []
    for idx, row in enumerate(rows):
        if idx < header_idx:
            continue
        if idx == header_idx:
            table_boxes.extend(row)
            continue
        if _is_section_title_row(row, img_w):
            continue
        texts = _recognize_row_boxes(image, row)
        if _is_paren_annotation_row(texts):
            continue
        table_boxes.extend(row)

    col_bounds_crop = [(l - tx1, r - tx1) for l, r in col_bounds_abs]
    line_boxes_crop = [
        (x1 - tx1, y1 - ty1, x2 - tx1, y2 - ty1)
        for x1, y1, x2, y2 in table_boxes
        if y1 >= ty1 - 2 and y2 <= ty2 + 2
    ]

    cells = _reconstruct_table_from_lines(
        image,
        [tx1, ty1, tx2, ty2],
        [],
        "",
        col_bounds_override=col_bounds_crop,
        line_boxes_override=line_boxes_crop,
    )
    if not cells:
        return None

    cells = _postprocess_sso_cells(cells)

    max_row = max(c.row for c in cells)
    max_col = max(c.col for c in cells)
    logger.info(
        "Page %d: SSO table %d×%d (%d cells, VietOCR)",
        page_number,
        max_row + 1,
        max_col + 1,
        len(cells),
    )
    return TableData(
        table_index=0,
        num_rows=max_row + 1,
        num_cols=max_col + 1,
        cells=cells,
        html="",
        table_kind="sso_agribank",
    )


def _fallback_full_page_ocr(
    image: np.ndarray, page_number: int
) -> TableData | None:
    """
    Fallback when PP-Structure is unavailable.

    Prefers SSO table pipeline (det + VietOCR); generic grid as last resort.
    """
    try:
        table = _fallback_sso_grid_lines_ocr(image, page_number)
        if table is not None:
            return table

        table = _fallback_sso_table_ocr(image, page_number)
        if table is not None:
            return table

        line_boxes = _detect_lines_in_region(image)
        if not line_boxes:
            return None

        img_h, img_w = image.shape[:2]
        # Exclude top letterhead (~12% page height).
        content_boxes = [
            b for b in line_boxes if b[1] >= int(img_h * 0.12)
        ]
        if not content_boxes:
            content_boxes = line_boxes

        ty1 = min(b[1] for b in content_boxes)
        ty2 = max(b[3] for b in content_boxes)
        tx1 = max(0, min(b[0] for b in content_boxes) - 8)
        tx2 = min(img_w, max(b[2] for b in content_boxes) + 8)

        centers = sorted((b[0] + b[2]) / 2 for b in content_boxes)
        num_cols = min(9, max(4, _estimate_column_count(centers)))
        col_bounds = _cluster_centers_to_bounds(centers, num_cols)
        col_bounds_crop = [(l - tx1, r - tx1) for l, r in col_bounds]
        line_boxes_crop = [
            (x1 - tx1, y1 - ty1, x2 - tx1, y2 - ty1)
            for x1, y1, x2, y2 in content_boxes
        ]

        cells = _reconstruct_table_from_lines(
            image,
            [tx1, ty1, tx2, ty2],
            [],
            "",
            col_bounds_override=col_bounds_crop,
            line_boxes_override=line_boxes_crop,
        )
        if not cells:
            return None

        max_row = max(c.row for c in cells)
        max_col = max(c.col for c in cells)
        return TableData(
            table_index=0,
            num_rows=max_row + 1,
            num_cols=max_col + 1,
            cells=cells,
            html="",
        )

    except Exception as e:
        logger.error("Fallback OCR failed on page %d: %s", page_number, e)
        return None


def _estimate_column_count(centers: list[float]) -> int:
    """Estimate column count from x-centre gaps (for generic fallback)."""
    if len(centers) < 4:
        return 4
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    med_gap = float(np.median(gaps))
    if med_gap <= 0:
        return 6
    big_gaps = sum(1 for g in gaps if g > med_gap * 1.8)
    return max(4, min(9, big_gaps + 1))
