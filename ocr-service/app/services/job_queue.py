"""
OCR job queue — FIFO, bounded, multi-worker.

Tránh spawn thread không giới hạn khi nhiều user upload cùng lúc.
N worker xử lý song song (CPU VietOCR); GPU Paddle serialize qua lock.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import settings
from app.models.schemas import JobStatus, ProcessingMode, RemoteProvider

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """Hàng đợi OCR đầy — từ chối upload mới."""


@dataclass
class OcrJobTask:
    job_id: str
    pdf_path: Path
    processing_mode: ProcessingMode
    api_provider: str
    use_gpu: bool
    remote_provider: RemoteProvider | None
    remote_url: str
    remote_token: str


class OcrJobQueue:
    def __init__(
        self,
        max_queue: int,
        worker_count: int,
        runner: Callable[[OcrJobTask], None],
    ) -> None:
        self._max_queue = max_queue
        self._runner = runner
        self._queue: queue.Queue[OcrJobTask | None] = queue.Queue(maxsize=max_queue)
        self._lock = threading.Lock()
        self._waiting_ids: list[str] = []
        self._active_job_id: str | None = None
        self._workers: list[threading.Thread] = []

        for idx in range(worker_count):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"ocr-queue-{idx}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    def submit(self, task: OcrJobTask) -> int:
        """Enqueue task. Returns 1-based queue position."""
        from app.services.table_service import get_job, update_job_queue_position

        try:
            self._queue.put(task, block=False)
        except queue.Full as exc:
            raise QueueFullError(
                f"Hàng đợi OCR đầy (tối đa {self._max_queue} job chờ). Thử lại sau."
            ) from exc

        with self._lock:
            self._waiting_ids.append(task.job_id)
            position = len(self._waiting_ids)
            if self._active_job_id:
                position += 1

        job = get_job(task.job_id)
        if job:
            job.status = JobStatus.QUEUED
            update_job_queue_position(task.job_id, position)

        logger.info(
            "[%s] Enqueued (position=%d, depth=%d)",
            task.job_id,
            position,
            self.queue_depth(),
        )
        return position

    def _worker_loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                break
            try:
                with self._lock:
                    if task.job_id in self._waiting_ids:
                        self._waiting_ids.remove(task.job_id)
                    self._active_job_id = task.job_id
                self._runner(task)
            except Exception:
                logger.exception("[%s] Queue worker failed", task.job_id)
            finally:
                with self._lock:
                    self._active_job_id = None
                self._queue.task_done()

    def queue_depth(self) -> int:
        with self._lock:
            depth = len(self._waiting_ids)
            if self._active_job_id:
                depth += 1
            return depth

    def stats(self) -> dict:
        with self._lock:
            return {
                "max_queue": self._max_queue,
                "waiting_count": len(self._waiting_ids),
                "active_job_id": self._active_job_id,
                "waiting_job_ids": list(self._waiting_ids),
                "queue_depth": len(self._waiting_ids)
                + (1 if self._active_job_id else 0),
            }


_queue: OcrJobQueue | None = None


def start_job_queue(runner: Callable[[OcrJobTask], None]) -> OcrJobQueue:
    global _queue
    if _queue is None:
        _queue = OcrJobQueue(
            max_queue=settings.ocr_queue_max_size,
            worker_count=settings.ocr_worker_threads,
            runner=runner,
        )
        logger.info(
            "OCR job queue started (max=%d, workers=%d)",
            settings.ocr_queue_max_size,
            settings.ocr_worker_threads,
        )
    return _queue


def get_job_queue() -> OcrJobQueue:
    if _queue is None:
        raise RuntimeError("OCR job queue chưa khởi động")
    return _queue
