"""ShiftMind FastAPI server — entrypoint.

Run:
    uv run uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI

from api import detect_face, faces, health, preview

app = FastAPI(title="ShiftMind server", version="0.1.0")
app.include_router(health.router)
app.include_router(detect_face.router)
app.include_router(faces.router)
app.include_router(preview.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "shiftmind-server",
        "version": "0.1.0",
        "endpoints": [
            "/api/health",
            "/api/detect-face",
            "/api/enroll-face",
            "/api/faces.html",
            "/api/preview",
            "/api/preview-face",
            "/api/preview-hud",
            "/api/preview.html",
            "/api/preview-frame",
            "/api/hud-state",
            "/api/preview-status",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
