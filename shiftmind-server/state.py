"""Shared in-process state. Phase-1: latest preview frame only."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class LatestFrame:
    """Single-slot frame buffer for the MJPEG preview pipeline.

    Producer (POST /api/preview-frame) overwrites; consumer (GET /api/preview)
    reads the latest. No queue — we want the freshest frame, not a backlog.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _jpeg: bytes | None = None
    _ts: float = 0.0
    _seq: int = 0

    def write(self, jpeg: bytes) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._ts = time.monotonic()
            self._seq += 1

    def read(self) -> tuple[bytes | None, float, int]:
        with self._lock:
            return self._jpeg, self._ts, self._seq


latest_frame = LatestFrame()
