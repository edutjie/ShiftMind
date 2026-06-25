"""ShiftMind FastAPI server — entrypoint.

Run:
    uv run uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from dotenv import load_dotenv
from fastapi import FastAPI

from api import detect_face, faces, health, preview, sessions
from session_service import session_service
from storage import storage

load_dotenv()

app = FastAPI(title="ShiftMind server", version="0.1.0")


@app.on_event("startup")
async def _clear_stale_sessions() -> None:
    """Abort sessions left 'active' by a previous run so the glasses don't
    re-confirm the last customer the moment the app reconnects."""
    aborted = storage.abort_active_sessions()
    session_service.reset()
    if aborted:
        print(f"[startup] aborted {aborted} stale active session(s)")


app.include_router(health.router)
app.include_router(detect_face.router)
app.include_router(faces.router)
app.include_router(preview.router)
app.include_router(sessions.router)


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
            "/api/session-status",
            "/api/session/{session_id}/audio-stream",
            "/api/session/{session_id}/finish",
            "/api/sessions",
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
