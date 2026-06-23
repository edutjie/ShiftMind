"""Feed the laptop webcam into /api/preview-frame.

Dev tool. Validates the server's preview pipeline end-to-end before the
glasses Camera2 client lands (phase 2). Once the glasses send real frames,
this stops being useful — but is harmless to keep.

    uv run python tools/feed_webcam.py
    uv run python tools/feed_webcam.py --url http://192.168.1.50:8000 --fps 10
"""
from __future__ import annotations

import argparse
import sys
import time
from urllib.parse import urljoin

import cv2
import requests


def main() -> int:
    p = argparse.ArgumentParser(description="Feed webcam frames to ShiftMind server preview.")
    p.add_argument("--url", default="http://localhost:8000",
                   help="ShiftMind server base URL (default: http://localhost:8000)")
    p.add_argument("--fps", type=float, default=5.0, help="Target frames per second (default: 5)")
    p.add_argument("--device", type=int, default=0, help="cv2 VideoCapture device index (default: 0)")
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100 (default: 80)")
    args = p.parse_args()

    endpoint = urljoin(args.url.rstrip("/") + "/", "api/preview-frame")
    interval = 1.0 / args.fps

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print(f"error: could not open camera device {args.device}", file=sys.stderr)
        return 1

    print(f"streaming device {args.device} -> {endpoint} at {args.fps} fps (ctrl-c to stop)")
    session = requests.Session()
    sent = 0
    errs = 0
    next_tick = time.monotonic()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                errs += 1
                time.sleep(0.05)
                continue

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), args.quality])
            if not ok:
                errs += 1
                continue

            try:
                r = session.post(
                    endpoint,
                    files={"image": ("frame.jpg", buf.tobytes(), "image/jpeg")},
                    timeout=2.0,
                )
                if r.status_code != 200:
                    errs += 1
                    print(f"server returned {r.status_code}: {r.text[:200]}", file=sys.stderr)
                else:
                    sent += 1
                    if sent % 50 == 0:
                        print(f"sent {sent} frames ({errs} errors)")
            except requests.RequestException as e:
                errs += 1
                print(f"post failed: {e}", file=sys.stderr)

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # Falling behind — resync to wall clock instead of running flat out.
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        print(f"\nstopped. sent={sent} errors={errs}")
    finally:
        cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
