"""Customer session APIs: voice upload, summary, memory update."""
from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from doubao_asr import DoubaoSeedAsrStream
from session_service import session_service
from state import latest_recognition_state
from storage import storage

router = APIRouter()


@router.get("/api/session-status")
async def session_status() -> dict:
    active = session_service.active()
    live = session_service.live_transcript(active.session_id) if active else None
    return {"active_session": active.to_dict() if active else None, "live_transcript": live}


@router.websocket("/api/session/{session_id}/audio-stream")
async def audio_stream(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    active = session_service.active()
    if not active or active.session_id != session_id:
        await websocket.send_text(json.dumps({"type": "error", "message": "session_not_active"}))
        await websocket.close(code=1008)
        return

    asr = DoubaoSeedAsrStream()
    if not asr.configured:
        await websocket.send_text(json.dumps({"type": "asr_status", "status": "missing_doubao_env"}))
        await _drain_without_asr(websocket)
        return

    try:
        async with asr:
            await websocket.send_text(json.dumps({"type": "asr_status", "status": "connected"}))
            recv_task = asyncio.create_task(_recv_doubao_loop(websocket, asr, session_id))
            try:
                while True:
                    message = await websocket.receive()
                    if message.get("bytes") is not None:
                        await asr.send_audio(message["bytes"])
                    elif message.get("text") == "eof":
                        await asr.send_audio(b"", final=True)
                        # Wait a bit for final transcripts from Doubao before closing
                        await asyncio.sleep(2.0)
                        break
            except WebSocketDisconnect:
                with contextlib.suppress(Exception):
                    await asr.send_audio(b"", final=True)
            finally:
                recv_task.cancel()
                with contextlib.suppress(Exception):
                    await recv_task
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "asr_status", "status": f"error:{type(exc).__name__}"}))


async def _recv_doubao_loop(websocket: WebSocket, asr: DoubaoSeedAsrStream, session_id: str) -> None:
    while True:
        item = await asr.recv_text()
        if not item:
            continue
        text, final = item
        live = session_service.append_live_transcript(session_id, text, final=final)
        await websocket.send_text(json.dumps({
            "type": "transcript",
            "text": text,
            "final": final,
            "final_text": live.get("final_text", ""),
            "partial_text": live.get("partial_text", ""),
        }, ensure_ascii=False))


async def _drain_without_asr(websocket: WebSocket) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("text") == "eof":
                break
    except WebSocketDisconnect:
        return


@router.post("/api/session/{session_id}/finish")
async def finish_session(
    session_id: str,
    audio: UploadFile | None = File(default=None),
    transcript: str = Form(default=""),
) -> dict:
    audio_bytes = await audio.read() if audio is not None else None
    live = session_service.live_transcript(session_id)
    transcript = transcript.strip() or " ".join(
        part for part in [str(live.get("final_text") or ""), str(live.get("partial_text") or "")] if part.strip()
    ).strip()
    try:
        result = await asyncio.to_thread(
            session_service.finish,
            session_id=session_id,
            audio_bytes=audio_bytes,
            transcript_hint=transcript,
        )
        latest_recognition_state.write({"recognition_status": "idle"})
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/api/sessions")
async def sessions() -> dict:
    return {
        "active_session": session_service.active().to_dict() if session_service.active() else None,
        "recent_sessions": storage.recent_sessions(20),
    }
