"""Shared in-process state for dev preview surfaces."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


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


@dataclass
class LatestFaceCrop:
    """Single-slot face crop buffer posted by the glasses face gate."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _jpeg: bytes | None = None
    _box: str | None = None
    _ts: float = 0.0
    _seq: int = 0

    def write(self, jpeg: bytes, box: str | None = None) -> None:
        with self._lock:
            self._jpeg = jpeg
            self._box = box
            self._ts = time.monotonic()
            self._seq += 1

    def read(self) -> tuple[bytes | None, str | None, float, int]:
        with self._lock:
            return self._jpeg, self._box, self._ts, self._seq


latest_face_crop = LatestFaceCrop()


@dataclass
class LatestHudState:
    """Single-slot HUD state buffer posted by the glasses.

    Keep this separate from latest_frame so model inputs can stay raw while
    browser preview gets a late human-only HUD overlay.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: dict[str, Any] | None = None
    _ts: float = 0.0
    _seq: int = 0

    def write(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = state
            self._ts = time.monotonic()
            self._seq += 1

    def read(self) -> tuple[dict[str, Any] | None, float, int]:
        with self._lock:
            return self._state, self._ts, self._seq


latest_hud_state = LatestHudState()


@dataclass
class LatestRecognitionState:
    """Latest server-side face recognition result."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: dict[str, Any] = field(default_factory=lambda: {"recognition_status": "idle"})
    _ts: float = 0.0
    _seq: int = 0

    def write(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = state
            self._ts = time.monotonic()
            self._seq += 1

    def read(self) -> tuple[dict[str, Any], float, int]:
        with self._lock:
            return self._state, self._ts, self._seq


latest_recognition_state = LatestRecognitionState()
