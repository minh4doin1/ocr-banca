"""
VietOCR GPU worker — chạy trong process riêng.

Process này chỉ load torch CUDA + VietOCR, không import PaddlePaddle.
Giao tiếp qua stdin/stdout (JSON lines).

Chạy thủ công:
  venv-vietocr-gpu\\Scripts\\python.exe -m app.services.vietocr_gpu_worker
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import traceback

logger = logging.getLogger("vietocr-gpu-worker")


def _emit(obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _decode_image(b64: str):
    from PIL import Image

    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _load_predictor(model_name: str):
    import torch
    from vietocr.tool.config import Cfg
    from vietocr.tool.predictor import Predictor

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA không khả dụng trong worker process")

    device = "cuda:0"
    config = Cfg.load_config_from_name(model_name)
    config["cnn"]["pretrained"] = True
    config["device"] = device
    config["predictor"]["beamsearch"] = False
    predictor = Predictor(config)
    return predictor, device


def run_worker_loop() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stderr,
    )

    model_name = os.environ.get("VIETOCR_MODEL", "vgg_transformer")
    logger.info("Loading VietOCR (%s) on GPU…", model_name)
    predictor, device = _load_predictor(model_name)
    logger.info("VietOCR ready on %s", device)
    _emit({"event": "ready", "device": device, "model": model_name})

    for raw_line in sys.stdin.buffer:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            cmd = req.get("cmd", "")

            if cmd == "ping":
                _emit({"id": req_id, "ok": True, "pong": True, "device": device})
                continue

            if cmd == "shutdown":
                _emit({"id": req_id, "ok": True})
                break

            if cmd == "predict_batch":
                images_b64 = req.get("images") or []
                pil_imgs = [_decode_image(b) for b in images_b64]
                texts = predictor.predict_batch(pil_imgs)
                _emit(
                    {
                        "id": req_id,
                        "ok": True,
                        "texts": [str(t).strip() for t in texts],
                    }
                )
                continue

            _emit({"id": req_id, "ok": False, "error": f"Unknown cmd: {cmd}"})
        except Exception as exc:
            logger.error("Worker error: %s", exc)
            _emit(
                {
                    "id": req_id,
                    "ok": False,
                    "error": str(exc),
                    "trace": traceback.format_exc()[-800:],
                }
            )


def main() -> None:
    try:
        run_worker_loop()
    except Exception as exc:
        logger.exception("Worker fatal: %s", exc)
        _emit({"event": "fatal", "error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
