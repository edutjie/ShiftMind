"""Active-login state + light PIN hashing for ShiftMind multi-employee support.

Single-slot in-memory holder (same pattern as state.LatestFrame): one active
employee/location per server instance — the worker currently on shift. The data
model in storage.py supports many employees/locations; at runtime a single
login drives session attribution. Concurrent multi-employee is future work.

PINs are optional (name-select-only is allowed). Hashing is dep-free
(hashlib.pbkdf2_hmac) so no passlib/python-jose dependency is added.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActiveLogin:
    employee_id: str
    employee_name: str
    location_id: str | None
    location_name: str | None
    login_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": True,
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "location_id": self.location_id,
            "location_name": self.location_name,
            "login_at": self.login_at,
        }


@dataclass
class ActiveLoginSlot:
    """Single-slot holder for the current shift's login (mirrors state.LatestFrame)."""

    _lock: threading.Lock = threading.Lock()
    _login: ActiveLogin | None = None

    def set(
        self,
        *,
        employee_id: str,
        employee_name: str,
        location_id: str | None = None,
        location_name: str | None = None,
    ) -> ActiveLogin:
        login = ActiveLogin(
            employee_id=employee_id,
            employee_name=employee_name,
            location_id=location_id,
            location_name=location_name,
            login_at=time.time(),
        )
        with self._lock:
            self._login = login
        return login

    def clear(self) -> None:
        with self._lock:
            self._login = None

    def read(self) -> ActiveLogin | None:
        with self._lock:
            return self._login

    def to_dict(self) -> dict[str, Any]:
        """Compact dict for embedding in API responses (preview-frame, whoami)."""
        with self._lock:
            login = self._login
        return login.to_dict() if login else {"active": False}


active_login = ActiveLoginSlot()


# ── PIN hashing (dep-free) ────────────────────────────────────────────────
# Format: "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>". Empty pin_hash on
# the employee row means name-select-only (no secret) — verify_pin returns True.

_ITERATIONS = 120_000
_PREFIX = "pbkdf2_sha256"


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, _ITERATIONS)
    return f"{_PREFIX}${_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_pin(pin: str | None, stored: str | None) -> bool:
    """True if the employee has no pin (name-select) or the pin matches."""
    if not stored:
        return True
    if not pin:
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != _PREFIX:
        return False
    iters = int(iters_s)
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    derived = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iters)
    return hmac.compare_digest(derived, expected)
