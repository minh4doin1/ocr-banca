"""Audit log cho mỗi request proxy."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("kc-proxy.audit")


def log_request(
    *,
    request_id: str,
    source_ip: str,
    method: str,
    kc_path: str,
    status: int,
    latency_ms: float,
) -> None:
    """Ghi 1 dòng audit log. Format dễ grep cho ELK/Splunk."""
    logger.info(
        "rid=%s src=%s method=%s path=%s status=%s latency_ms=%.1f",
        request_id,
        source_ip,
        method,
        kc_path,
        status,
        latency_ms,
    )


class Timer:
    """Context manager đo latency."""

    def __init__(self) -> None:
        self.elapsed_ms: float = 0.0
        self._t0: float = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000