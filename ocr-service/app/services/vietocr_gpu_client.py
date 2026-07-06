"""
Client quản lý VietOCR GPU worker (process con, torch CUDA).

Paddle GPU chạy trong process chính; VietOCR chạy torch CUDA trong
venv-vietocr-gpu riêng để tránh xung đột pybind/CUDA.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLIENT: VietOcrGpuClient | None = None
_CLIENT_LOCK = threading.Lock()


def _resolve_worker_python() -> Path | None:
    explicit = (settings.vietocr_gpu_python or "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    bundled = _PROJECT_ROOT / "venv-vietocr-gpu" / "Scripts" / "python.exe"
    if bundled.exists():
        return bundled
    return None


class VietOcrGpuClient:
    """JSONL IPC (UTF-8 binary) tới subprocess VietOCR GPU."""

    def __init__(self, python_exe: Path) -> None:
        self._python = python_exe
        self._proc: subprocess.Popen[bytes] | None = None
        self._io_lock = threading.Lock()
        self._ready = False
        self._device = ""
        self._stderr_thread: threading.Thread | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready and self._proc is not None and self._proc.poll() is None

    @property
    def device(self) -> str:
        return self._device

    def start(self, *, timeout: float = 180.0) -> bool:
        if self.is_ready:
            return True
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        env["VIETOCR_MODEL"] = settings.vietocr_model
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self._proc = subprocess.Popen(
                [str(self._python), "-m", "app.services.vietocr_gpu_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=str(_PROJECT_ROOT),
                env=env,
            )
        except Exception as exc:
            logger.warning("Không khởi động VietOCR GPU worker: %s", exc)
            return False

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="vietocr-gpu-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        deadline = time.monotonic() + timeout
        assert self._proc.stdout is not None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                logger.error(
                    "VietOCR GPU worker thoát sớm (code=%s)", self._proc.returncode
                )
                return False
            raw = self._proc.stdout.readline()
            if not raw:
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if msg.get("event") == "ready":
                self._ready = True
                self._device = msg.get("device", "cuda:0")
                logger.info(
                    "VietOCR GPU worker sẵn sàng (%s, model=%s)",
                    self._device,
                    msg.get("model", settings.vietocr_model),
                )
                return True
            if msg.get("event") == "fatal":
                logger.error("VietOCR GPU worker fatal: %s", msg.get("error"))
                return False

        logger.error("VietOCR GPU worker timeout sau %.0fs", timeout)
        self.shutdown()
        return False

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("[vietocr-gpu] %s", text)

    def _write_line(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _read_line(self) -> dict | None:
        assert self._proc is not None and self._proc.stdout is not None
        raw = self._proc.stdout.readline()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", errors="replace"))

    def _request(self, payload: dict, *, timeout: float = 300.0) -> dict:
        if not self.is_ready:
            raise RuntimeError("VietOCR GPU worker chưa sẵn sàng")
        assert self._proc is not None

        req_id = payload.setdefault("id", uuid.uuid4().hex)
        with self._io_lock:
            if self._proc.poll() is not None:
                raise RuntimeError("VietOCR GPU worker đã dừng")

            self._write_line(payload)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                msg = self._read_line()
                if msg is None:
                    if self._proc.poll() is not None:
                        raise RuntimeError("VietOCR GPU worker crash mid-request")
                    time.sleep(0.01)
                    continue
                if msg.get("id") == req_id:
                    return msg
            raise TimeoutError("VietOCR GPU worker không phản hồi")

    def ping(self) -> bool:
        try:
            resp = self._request({"cmd": "ping"}, timeout=30.0)
            return bool(resp.get("ok"))
        except Exception:
            return False

    def predict_batch(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        images_b64: list[str] = []
        for crop in crops:
            pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            images_b64.append(base64.b64encode(buf.getvalue()).decode("ascii"))

        resp = self._request(
            {"cmd": "predict_batch", "images": images_b64},
            timeout=max(120.0, len(crops) * 2.0),
        )
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "predict_batch failed")
        texts = resp.get("texts") or []
        return [(str(t).strip(), _estimate_confidence_local(t)) for t in texts]

    def shutdown(self) -> None:
        self._ready = False
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and proc.poll() is None:
                data = (json.dumps({"cmd": "shutdown", "id": "bye"}) + "\n").encode(
                    "utf-8"
                )
                proc.stdin.write(data)
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logger.info("VietOCR GPU worker stopped")


def _estimate_confidence_local(text: str) -> float:
    if not text or not text.strip():
        return 0.0
    text = text.strip()
    if len(text) <= 1:
        return 0.7
    vietnamese = set(
        "àáảãạăắằẳẵặâấầẩẫậèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵđ"
    )
    if any(c.lower() in vietnamese for c in text):
        return 0.92
    if text.isdigit():
        return 0.95
    if "@" in text:
        return 0.88
    return 0.85


def get_vietocr_gpu_client(*, auto_start: bool = True) -> VietOcrGpuClient | None:
    """Return singleton GPU client when enabled and worker venv exists."""
    global _CLIENT
    if not settings.vietocr_gpu_subprocess or not settings.paddle_use_gpu:
        return None
    with _CLIENT_LOCK:
        if _CLIENT is None:
            py = _resolve_worker_python()
            if py is None:
                logger.warning(
                    "VIETOCR GPU subprocess bật nhưng chưa có venv-vietocr-gpu — "
                    "chạy scripts/setup_vietocr_gpu_worker.ps1"
                )
                return None
            _CLIENT = VietOcrGpuClient(py)
        if auto_start and not _CLIENT.is_ready:
            if not _CLIENT.start():
                return None
        return _CLIENT if _CLIENT.is_ready else None


def shutdown_vietocr_gpu_client() -> None:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.shutdown()
            _CLIENT = None


def warmup_vietocr_gpu_worker() -> bool:
    client = get_vietocr_gpu_client(auto_start=True)
    return client is not None and client.ping()
