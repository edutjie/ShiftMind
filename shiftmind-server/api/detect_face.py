"""POST /api/detect-face — mock customer recognition.

Phase-1 stub. Real implementation in phase 3 will:
  - decode JPEG
  - insightface embed
  - FAISS top-1 over known customers
  - return memory card from SQLite

For now: returns a fixed mock customer, OR a new-customer payload when the
request includes `X-Mock-New: 1`. Lets the glasses client exercise both code
paths without server-side changes.
"""
from __future__ import annotations

from fastapi import APIRouter, File, Header, UploadFile

router = APIRouter()


MOCK_KNOWN = {
    "customer_id": "mock-001",
    "name": "John D.",
    "preferences": ["red shoes", "size 10"],
    "memory_card": "Last visit: bought running shoes 3 weeks ago.",
    "last_visit": "2026-06-01",
    "confidence": 0.92,
}

MOCK_NEW = {"new_customer": True, "customer_id": "mock-new-001"}


@router.post("/api/detect-face")
async def detect_face(
    image: UploadFile = File(...),
    x_mock_new: str | None = Header(default=None, alias="X-Mock-New"),
) -> dict:
    # Drain the upload so the client doesn't see a half-read body.
    payload = await image.read()
    _ = len(payload)  # phase-1: ignored; real impl will decode JPEG here
    if x_mock_new == "1":
        return MOCK_NEW
    return MOCK_KNOWN
