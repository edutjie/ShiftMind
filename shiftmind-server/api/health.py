"""GET /api/health — liveness probe for the glasses connectivity check."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

VERSION = "0.1.0"


@router.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": VERSION}
