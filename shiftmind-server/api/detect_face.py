"""POST /api/detect-face — real customer recognition."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, UploadFile

from face_recognition_service import get_face_service
from state import latest_recognition_state

router = APIRouter()


@router.post("/api/detect-face")
async def detect_face(image: UploadFile = File(...)) -> dict:
    payload = await image.read()
    result = await asyncio.to_thread(get_face_service().recognize, payload)
    data = result.to_dict()
    latest_recognition_state.write(data)
    return data
