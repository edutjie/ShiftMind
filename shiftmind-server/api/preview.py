"""Dev-only live preview pipeline.

  POST /api/preview-frame   producer  (multipart JPEG → in-memory slot)
  GET  /api/preview         consumer  (MJPEG stream: multipart/x-mixed-replace)
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
from fastapi import APIRouter, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from state import latest_frame

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


def _rotate_jpeg(jpeg: bytes) -> bytes:
    """Decode → rotate → re-encode. cv2 handles JPEG in/out natively.

    Cost on a 640x480 frame is ~3 ms each on a 2024 laptop CPU; trivial next
    to the 100 ms frame interval. If we ever care, swap to lossless transpose
    on the JPEG bitstream directly (`jpegtran` or `pyturbojpeg`).
    """
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return jpeg  # decode failed; fall back to raw bytes
    rotated = cv2.rotate(img, PREVIEW_ROTATE_CV2)
    ok, buf = cv2.imencode(".jpg", rotated, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else jpeg


@router.post("/api/preview-frame")
async def preview_frame(image: UploadFile = File(...)) -> dict:
    """Producer endpoint. Glasses (or feed_webcam.py) POST JPEG frames here."""
    jpeg = await image.read()
    if not jpeg:
        return {"received": False, "reason": "empty"}
    latest_frame.write(jpeg)
    return {"received": True, "bytes": len(jpeg)}


@router.get("/api/preview-status")
async def preview_status() -> dict:
    jpeg, ts, seq = latest_frame.read()
    return {
        "have_frame": jpeg is not None,
        "seq": seq,
        "age_seconds": (time.monotonic() - ts) if jpeg is not None else None,
    }


def _mjpeg_chunk(jpeg: bytes) -> bytes:
    return (
        b"--" + BOUNDARY + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
        + jpeg + b"\r\n"
    )


async def _mjpeg_generator():
    last_seq = -1
    cached_rotated: bytes | None = None  # rotate once per fresh frame, reuse if no new seq
    while True:
        jpeg, _, seq = latest_frame.read()
        if jpeg is None:
            yield _mjpeg_chunk(_PLACEHOLDER_JPEG)
        elif seq != last_seq:
            cached_rotated = _rotate_jpeg(jpeg)
            yield _mjpeg_chunk(cached_rotated)
            last_seq = seq
        # else: same frame as last tick, skip the write to avoid wasted bandwidth
        await asyncio.sleep(FRAME_INTERVAL)


@router.get("/api/preview")
async def preview():
    return StreamingResponse(
        _mjpeg_generator(),
        media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
    )


@router.get("/api/preview.html")
async def preview_html() -> HTMLResponse:
    """Tiny convenience page so you can just click a link."""
    html = """<!doctype html><html><head><title>ShiftMind preview</title>
<style>body{margin:0;background:#000;color:#0f0;font:14px monospace;display:flex;
flex-direction:column;align-items:center;padding:12px}
img{max-width:100%;height:auto;border:1px solid #0f0}</style></head>
<body><h3>shiftmind live preview</h3>
<img src="/api/preview" alt="live MJPEG stream" />
<p>POST JPEGs to <code>/api/preview-frame</code> to feed this.</p>
</body></html>"""
    return HTMLResponse(html)
