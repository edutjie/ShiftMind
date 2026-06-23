"""ShiftMind FastAPI server — entrypoint.

Run:
    uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

from fastapi import FastAPI

from api import detect_face, health, preview

app = FastAPI(title="ShiftMind server", version="0.1.0")
app.include_router(health.router)
app.include_router(detect_face.router)
app.include_router(preview.router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "shiftmind-server",
        "version": "0.1.0",
        "endpoints": [
            "/api/health",
            "/api/detect-face",
            "/api/preview",
            "/api/preview.html",
            "/api/preview-frame",
            "/api/preview-status",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
