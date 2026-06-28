"""Dev-only live preview pipeline.

  POST /api/preview-frame   producer  (multipart JPEG → in-memory slot)
  POST /api/hud-state       producer  (glasses HUD state JSON)
  GET  /api/preview         consumer  (MJPEG stream: multipart/x-mixed-replace)
  GET  /api/preview-hud     consumer  (MJPEG stream with HUD overlay)
  GET  /api/preview-status  metadata  (latest seq + age in seconds)

Open `http://<host>:8000/api/preview` in a browser to watch the live feed.
Not in the architecture_v2.md spec endpoints — added for dev validation.

Preview rotation: the Rokid RG-glasses sensor reports `Orientation: 270`
(`adb shell dumpsys media.camera`), so raw frames arrive landscape with up
pointing right. We rotate 90° clockwise before serving the MJPEG so the
browser sees upright video. The producer stores the raw JPEG bytes — only
the preview rotates them, so future face-detection pipelines can choose
their own rotation policy without re-fetching.
"""
from __future__ import annotations

import asyncio
import time

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from auth import active_login
from face_recognition_service import get_face_service, normalize_face_crop_jpeg
from session_service import session_service
from state import latest_face_crop, latest_frame, latest_hud_state, latest_recognition_state
from storage import storage

router = APIRouter()

# Stream pacing. Browser MJPEG decoders cope fine with 10 fps; pushing higher
# wastes CPU since the producer probably feeds at 5 fps.
TARGET_FPS = 10.0
FRAME_INTERVAL = 1.0 / TARGET_FPS
BOUNDARY = b"frame"

# Camera sensor orientation on Rokid RG-glasses (from dumpsys media.camera).
# 270° means we rotate 90° clockwise on consume to make the picture upright.
# If a different Rokid model with different mounting ships later, expose this
# via env / per-frame metadata.
PREVIEW_ROTATE_CV2 = cv2.ROTATE_90_COUNTERCLOCKWISE
_recording_started_at: float | None = None


class CustomerCardPayload(BaseModel):
    name: str = ""
    lastVisit: str | None = None
    memoryLine: str | None = None
    preferences: list[str] = Field(default_factory=list)


class HudStatePayload(BaseModel):
    title: str = "ShiftMind"
    statusLine: str | None = None
    customerCard: CustomerCardPayload | None = None
    recordingActive: bool = False


def _make_placeholder_jpeg() -> bytes:
    """Black 320x240 frame with 'no signal' text, rendered once at import."""
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    cv2.putText(
        img, "no frames yet", (60, 130),
        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA,
    )
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok, "cv2 failed to encode placeholder JPEG"
    return buf.tobytes()


_PLACEHOLDER_JPEG = _make_placeholder_jpeg()


def _decode_and_rotate(jpeg: bytes) -> np.ndarray | None:
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.rotate(img, PREVIEW_ROTATE_CV2)


def _encode_jpeg(img: np.ndarray) -> bytes | None:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else None


def _preview_jpeg(jpeg: bytes, *, with_hud: bool) -> bytes:
    """Decode → rotate → optional HUD overlay → re-encode.

    Cost on a 640x480 frame is ~3 ms each on a 2024 laptop CPU; trivial next
    to the 100 ms frame interval. If we ever care, swap to lossless transpose
    on the JPEG bitstream directly (`jpegtran` or `pyturbojpeg`).
    """
    img = _decode_and_rotate(jpeg)
    if img is None:
        return jpeg  # decode failed; fall back to raw bytes
    if with_hud:
        _draw_hud_overlay(img)
    return _encode_jpeg(img) or jpeg


def _draw_hud_overlay(img: np.ndarray) -> None:
    """Approximate the Compose prism HUD on top of the web preview frame."""
    global _recording_started_at

    state, _, _ = latest_hud_state.read()
    if not state:
        state = {"title": "ShiftMind", "statusLine": "awaiting customer", "recordingActive": False}
    recog, _, _ = latest_recognition_state.read()
    active_session = session_service.active()
    if active_session:
        live = session_service.live_transcript(active_session.session_id)
        transcript_line = str(live.get("partial_text") or live.get("final_text") or "").strip()
        state = dict(state)
        state["statusLine"] = f"session {active_session.customer_id}"
        state["recordingActive"] = True
        state["customerCard"] = {
            "name": active_session.name if not active_session.is_new_customer else "First visit",
            "memoryLine": transcript_line[-140:] or active_session.memory_card or "listening",
        }
    elif recog.get("recognition_status") in {"confirmed", "unrecognized_first_visit"}:
        state = dict(state)
        if recog.get("recognition_status") == "confirmed":
            person_id = str(recog.get("person_id") or "")
            state["statusLine"] = f"confirmed {person_id}".strip()
            state["customerCard"] = {
                "name": recog.get("name") or person_id or "known customer",
                "memoryLine": recog.get("summary") or "Dummy summary: returning customer.",
            }
        else:
            state["statusLine"] = "unrecognized, first visit"
            state["customerCard"] = {
                "name": "First visit",
                "memoryLine": "unrecognized, first visit",
            }

    h, w = img.shape[:2]
    green = (0, 255, 0)
    white = (238, 238, 238)
    muted = (190, 190, 190)
    red = (58, 58, 255)

    title = str(state.get("title") or "ShiftMind")
    _center_text(img, title, y=int(h * 0.155), scale=_scale_for_width(w, 1.25), color=green, thickness=2)

    status = state.get("statusLine")
    if status:
        _center_text(img, str(status), y=int(h * 0.205), scale=_scale_for_width(w, 0.46), color=muted, thickness=1)

    recording_active = bool(state.get("recordingActive"))
    if recording_active and _recording_started_at is None:
        _recording_started_at = time.monotonic()
    elif not recording_active:
        _recording_started_at = None

    if recording_active:
        elapsed = int(time.monotonic() - (_recording_started_at or time.monotonic()))
        rec = f"REC {elapsed // 60:02d}:{elapsed % 60:02d}"
        rec_scale = _scale_for_width(w, 0.46)
        rec_size, _ = cv2.getTextSize(rec, cv2.FONT_HERSHEY_SIMPLEX, rec_scale, 1)
        rec_x = max(8, w - rec_size[0] - 18)
        rec_y = int(h * 0.095)
        cv2.circle(img, (rec_x - 12, rec_y - 5), 5, red, -1, cv2.LINE_AA)
        cv2.putText(img, rec, (rec_x, rec_y), cv2.FONT_HERSHEY_SIMPLEX, rec_scale, red, 1, cv2.LINE_AA)

    card = state.get("customerCard")
    if isinstance(card, dict):
        name = str(card.get("name") or "").strip()
        memory = str(card.get("memoryLine") or "").strip()
        if name:
            card_w = min(w - 48, int(w * 0.74))
            x0 = max(24, (w - card_w) // 2)
            y0 = int(h * 0.235)
            y1 = y0 + (int(h * 0.11) if memory else int(h * 0.07))
            overlay = img.copy()
            cv2.rectangle(overlay, (x0, y0), (x0 + card_w, y1), (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.42, img, 0.58, 0, img)
            cv2.rectangle(img, (x0, y0), (x0 + card_w, y1), green, 1)
            cv2.putText(
                img,
                name,
                (x0 + 14, y0 + int(h * 0.04)),
                cv2.FONT_HERSHEY_SIMPLEX,
                _scale_for_width(w, 0.55),
                green,
                1,
                cv2.LINE_AA,
            )
            if memory:
                _fit_text(
                    img,
                    memory,
                    (x0 + 14, y0 + int(h * 0.078)),
                    max_width=card_w - 28,
                    color=white,
                )

    # Compose BoxCursor is 240dp on a 480px-wide glasses surface: about half
    # viewport width. Web overlay uses the same proportion after rotation.
    box_w = int(w * 0.50)
    box_h = box_w
    cx, cy = w // 2, int(h * 0.58)
    x0, y0 = cx - box_w // 2, cy - box_h // 2
    x1, y1 = cx + box_w // 2, cy + box_h // 2
    corner = int(box_w * 0.18)
    stroke = max(2, int(round(w / 180)))
    for sx, sy in [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]:
        dx = corner if sx == x0 else -corner
        dy = corner if sy == y0 else -corner
        cv2.line(img, (sx, sy), (sx + dx, sy), green, stroke, cv2.LINE_AA)
        cv2.line(img, (sx, sy), (sx, sy + dy), green, stroke, cv2.LINE_AA)

    tick = max(12, int(w * 0.025))
    cv2.line(img, (cx - tick, cy), (cx + tick, cy), green, stroke, cv2.LINE_AA)
    cv2.line(img, (cx, cy - tick), (cx, cy + tick), green, stroke, cv2.LINE_AA)


def _scale_for_width(width: int, base_at_480: float) -> float:
    return max(0.25, base_at_480 * (width / 480.0))


def _center_text(img: np.ndarray, text: str, *, y: int, scale: float, color: tuple[int, int, int], thickness: int) -> None:
    size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x = max(0, (img.shape[1] - size[0]) // 2)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _fit_text(img: np.ndarray, text: str, origin: tuple[int, int], *, max_width: int, color: tuple[int, int, int]) -> None:
    scale = 0.38
    while scale > 0.24:
        size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        if size[0] <= max_width:
            break
        scale -= 0.03
    cv2.putText(img, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


@router.post("/api/preview-frame")
async def preview_frame(
    image: UploadFile = File(...),
    face_crop: UploadFile | None = File(default=None),
    face_box: str | None = Form(default=None),
) -> dict:
    """Producer endpoint. Glasses (or feed_webcam.py) POST JPEG frames here."""
    jpeg = await image.read()
    if not jpeg:
        return {"received": False, "reason": "empty"}
    latest_frame.write(jpeg)
    face_bytes = await face_crop.read() if face_crop is not None else b""
    recognition = None
    active_session = session_service.active()
    if face_bytes and active_session is None:
        face_bytes = normalize_face_crop_jpeg(face_bytes)
        latest_face_crop.write(face_bytes, face_box)
        result = await asyncio.to_thread(get_face_service().recognize, face_bytes)
        recognition = result.to_dict()
        active_session = await asyncio.to_thread(session_service.ensure_from_recognition, result, face_bytes)
        if active_session:
            recognition["active_session"] = active_session.to_dict()
        latest_recognition_state.write(recognition)
    return {
        "received": True,
        "bytes": len(jpeg),
        "face_crop": bool(face_bytes),
        "recognition": recognition or latest_recognition_state.read()[0],
        "active_session": active_session.to_dict() if active_session else None,
        "active_employee": active_login.to_dict(),
    }


@router.post("/api/hud-state")
async def hud_state(payload: HudStatePayload) -> dict:
    """Producer endpoint. Glasses POST current HUD state for web overlay."""
    latest_hud_state.write(payload.dict())
    return {"received": True}


@router.get("/api/preview-status")
async def preview_status() -> dict:
    jpeg, ts, seq = latest_frame.read()
    hud, hud_ts, hud_seq = latest_hud_state.read()
    face, face_box, face_ts, face_seq = latest_face_crop.read()
    recognition, recognition_ts, recognition_seq = latest_recognition_state.read()
    active_session = session_service.active()
    return {
        "have_frame": jpeg is not None,
        "seq": seq,
        "age_seconds": (time.monotonic() - ts) if jpeg is not None else None,
        "have_hud_state": hud is not None,
        "hud_seq": hud_seq,
        "hud_age_seconds": (time.monotonic() - hud_ts) if hud is not None else None,
        "have_face_crop": face is not None,
        "face_seq": face_seq,
        "face_box": face_box,
        "face_age_seconds": (time.monotonic() - face_ts) if face is not None else None,
        "recognition_seq": recognition_seq,
        "recognition_age_seconds": (time.monotonic() - recognition_ts) if recognition_ts else None,
        "active_session": active_session.to_dict() if active_session else None,
        "active_employee": active_login.to_dict(),
        **recognition,
    }


@router.get("/api/preview-session")
async def preview_session() -> dict:
    """Session info panel data for the preview page.

    Surfaces the active session's live transcript + the recognized customer's
    stored memory_card and important_points (the latter pulled from the most
    recent ended session row, since points are saved per-session).
    """
    import json

    active = session_service.active()
    if not active:
        return {"active": False}

    live = session_service.live_transcript(active.session_id)
    transcript_line = str(live.get("partial_text") or live.get("final_text") or "").strip()

    # important_points are stored per-session; pull the latest ended session's points.
    history = storage.get_customer_session_history(active.customer_id, limit=1)
    important_points: list[str] = []
    if history:
        try:
            important_points = json.loads(history[0].get("important_points_json") or "[]")
        except Exception:
            important_points = []

    return {
        "active": True,
        "session_id": active.session_id,
        "customer_id": active.customer_id,
        "name": active.name,
        "is_new_customer": active.is_new_customer,
        "memory_card": active.memory_card,
        "important_points": important_points,
        "live_transcript": transcript_line,
    }


def _mjpeg_chunk(jpeg: bytes) -> bytes:
    return (
        b"--" + BOUNDARY + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
        + jpeg + b"\r\n"
    )


async def _mjpeg_generator(*, with_hud: bool):
    last_seq = -1
    last_hud_seq = -1
    cached_rotated: bytes | None = None  # rotate once per fresh frame, reuse if no new seq
    while True:
        jpeg, _, seq = latest_frame.read()
        _, _, hud_seq = latest_hud_state.read()
        if jpeg is None:
            yield _mjpeg_chunk(_PLACEHOLDER_JPEG)
        elif seq != last_seq or (with_hud and hud_seq != last_hud_seq):
            cached_rotated = _preview_jpeg(jpeg, with_hud=with_hud)
            yield _mjpeg_chunk(cached_rotated)
            last_seq = seq
            last_hud_seq = hud_seq
        # else: same frame as last tick, skip the write to avoid wasted bandwidth
        await asyncio.sleep(FRAME_INTERVAL)


async def _face_crop_generator():
    last_seq = -1
    while True:
        jpeg, _, _, seq = latest_face_crop.read()
        if jpeg is None:
            yield _mjpeg_chunk(_PLACEHOLDER_JPEG)
        elif seq != last_seq:
            yield _mjpeg_chunk(jpeg)
            last_seq = seq
        await asyncio.sleep(FRAME_INTERVAL)


@router.get("/api/preview")
async def preview():
    return StreamingResponse(
        _mjpeg_generator(with_hud=False),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
    )


@router.get("/api/preview-hud")
async def preview_hud():
    return StreamingResponse(
        _mjpeg_generator(with_hud=True),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
    )


@router.get("/api/preview-face")
async def preview_face():
    return StreamingResponse(
        _face_crop_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
    )


@router.get("/api/preview.html")
async def preview_html() -> HTMLResponse:
    """Tiny convenience page so you can just click a link."""
    login = active_login.to_dict()
    if login.get("active"):
        login_line = (
            f"active employee: <b>{login['employee_name']}</b>"
            f" @ <b>{login.get('location_name') or '—'}</b>"
            f" · <a href='/api/login.html'>change</a>"
        )
    else:
        login_line = "<span class='idle'>no employee logged in</span> · <a href='/api/login.html'>log in</a>"
    html = """<!doctype html><html><head><title>ShiftMind preview</title>
<style>body{margin:0;background:#000;color:#0f0;font:14px monospace;display:flex;
flex-direction:column;align-items:center;padding:12px}
main{display:flex;gap:16px;align-items:flex-start;justify-content:center;flex-wrap:wrap}
section{display:flex;flex-direction:column;align-items:center;gap:8px}
img{max-width:100%;height:auto;border:1px solid #0f0}
.face{width:220px;min-height:160px;object-fit:contain}
.panel{width:300px;border:1px solid #0f0;padding:12px;text-align:left;box-sizing:border-box}
.panel h4{margin:0 0 8px;color:#0f0;border-bottom:1px solid #034;padding-bottom:4px}
.panel .label{color:#6f6;font-size:11px;margin-top:10px;text-transform:uppercase;letter-spacing:1px}
.panel .value{color:#eee;font-size:13px;margin-top:2px;word-wrap:break-word}
.panel ul{margin:4px 0 0;padding-left:18px}
.panel li{color:#cfc;font-size:12px;margin:2px 0}
.panel .idle{color:#666;font-style:italic}
.live{color:#fff !important}</style></head>
<body><h3>shiftmind live preview + HUD</h3><p>@@LOGIN@@</p>
<main>
  <section><strong>HUD preview</strong><img src="/api/preview-hud" alt="live MJPEG stream with HUD" /></section>
  <section><strong>face crop</strong><img class="face" src="/api/preview-face" alt="latest detected face crop" /></section>
  <section><strong>session info</strong>
    <div class="panel" id="session-panel">
      <h4>Session</h4>
      <div class="idle" id="session-body">no active session</div>
    </div>
  </section>
</main>
<p><a href="/api/preview">raw preview</a> · <a href="/api/faces.html">faces debug</a> · POST JPEGs to <code>/api/preview-frame</code> and HUD JSON to <code>/api/hud-state</code>.</p>
<script>
function esc(s){return String(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
async function refreshSession(){
  try{
    const r = await fetch("/api/preview-session");
    const d = await r.json();
    const body = document.getElementById("session-body");
    if(!d.active){
      body.className = "idle";
      body.textContent = "no active session";
      return;
    }
    body.className = "";
    const points = (d.important_points||[]).map(p=>`<li>${esc(p)}</li>`).join("");
    body.innerHTML =
      `<div class="label">Customer</div>`+
      `<div class="value">${esc(d.name)} (${esc(d.customer_id)})${d.is_new_customer?" · NEW":""}</div>`+
      `<div class="label">Memory Card</div>`+
      `<div class="value">${esc(d.memory_card)||"<span class=idle>none yet</span>"}</div>`+
      `<div class="label">Important Points</div>`+
      (points?`<ul>${points}</ul>`:`<div class="value idle">none yet</div>`)+
      `<div class="label">Live Transcript</div>`+
      `<div class="value live">${esc(d.live_transcript)||"<span class=idle>listening...</span>"}</div>`;
  }catch(e){ /* keep last good render */ }
}
refreshSession();
setInterval(refreshSession, 1000);
</script>
</body></html>"""
    return HTMLResponse(html.replace("@@LOGIN@@", login_line))
