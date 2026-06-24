"""Debug UI and enrollment API for face recognition."""
from __future__ import annotations

import asyncio
import html

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse

from face_recognition_service import face_service_status, get_face_service
from state import latest_face_crop, latest_recognition_state
from storage import storage

router = APIRouter()


@router.post("/api/enroll-face")
async def enroll_face(
    person_id: str = Form(default=""),
    name: str = Form(...),
    memory_card: str = Form(default="Dummy summary: returning customer."),
) -> dict:
    crop, _, _, _ = latest_face_crop.read()
    if not crop:
        raise HTTPException(status_code=400, detail="no latest face crop")
    customer_id = person_id.strip() or storage.next_customer_id()
    result = await asyncio.to_thread(
        get_face_service().enroll_latest_crop,
        customer_id=customer_id,
        name=name.strip() or customer_id,
        memory_card=memory_card.strip(),
        crop_jpeg=crop,
    )
    return {"ok": True, **result}


@router.get("/api/faces.html")
async def faces_html() -> HTMLResponse:
    recog, recog_ts, recog_seq = latest_recognition_state.read()
    _, face_box, face_ts, face_seq = latest_face_crop.read()
    customers = storage.list_customers()
    events = storage.recent_events(20)
    diag = face_service_status()
    customer_names = {c.id: c.name for c in customers}
    embedding_points = _embedding_points()

    rows = "\n".join(
        f"<tr><td>{_e(c.id)}</td><td>{_e(c.name)}</td><td>{_e(c.memory_card)}</td><td>{_e(c.updated_at)}</td></tr>"
        for c in customers
    ) or "<tr><td colspan='4'>no customers yet</td></tr>"
    event_rows = "\n".join(
        f"<tr><td>{_e(e['created_at'])}</td><td>{_e(e['status'])}</td><td>{_e(e.get('candidate_customer_id'))}</td>"
        f"<td>{_fmt(e.get('similarity'))}</td><td>{_fmt(e.get('margin'))}</td></tr>"
        for e in events
    ) or "<tr><td colspan='5'>no events yet</td></tr>"
    recog_items = "".join(f"<li><b>{_e(k)}</b>: {_e(v)}</li>" for k, v in recog.items())
    diag_items = "".join(f"<li><b>{_e(k)}</b>: {_e(v)}</li>" for k, v in diag.items())
    embedding_svg = _render_embedding_svg(embedding_points, customer_names)
    embedding_rows = "\n".join(
        f"<tr><td>{_e(_short(p.get('embedding_id')))}</td><td>{_e(p.get('customer_id'))}</td>"
        f"<td>{_e(customer_names.get(str(p.get('customer_id')), ''))}</td><td>{_e(p.get('model_name'))}</td>"
        f"<td>{_fmt(p.get('quality'))}</td><td>{_fmt(p.get('vector_norm'))}</td>"
        f"<td>{_e(p.get('created_at'))}</td></tr>"
        for p in embedding_points
    ) or "<tr><td colspan='7'>no embeddings yet</td></tr>"

    body = f"""<!doctype html>
<html><head><title>ShiftMind faces</title>
<style>
body{{background:#050505;color:#0f0;font:14px monospace;margin:0;padding:18px}}
a{{color:#9f9}} main{{display:grid;grid-template-columns:280px 1fr;gap:18px;align-items:start}}
section{{border:1px solid #0f0;padding:12px;min-width:0}} img{{max-width:240px;border:1px solid #0f0}}
input,textarea,button{{background:#000;color:#0f0;border:1px solid #0f0;font:14px monospace;padding:6px;width:100%;box-sizing:border-box}}
button{{cursor:pointer}} table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #063;padding:5px;text-align:left;vertical-align:top}}
svg{{width:100%;max-width:620px;height:auto;border:1px solid #063;background:#000}}
.muted{{color:#8f8}} .meta{{overflow-wrap:anywhere;word-break:break-word;line-height:1.25}}
</style></head>
<body>
<h2>ShiftMind faces</h2>
<p><a href="/api/preview.html">preview</a> · <a href="/api/preview-status">status json</a></p>
<main>
  <section>
    <h3>latest crop</h3>
    <img src="/api/preview-face" alt="latest face crop"/>
    <p class="meta">seq={face_seq}<br/>box={_e(face_box)}<br/>age={_fmt_age(face_ts)}</p>
    <form action="/api/enroll-face" method="post">
      <label>person_id</label><input name="person_id" placeholder="{storage.next_customer_id()}"/>
      <label>name</label><input name="name" required placeholder="Maya Chen"/>
      <label>memory_card</label><textarea name="memory_card" rows="4">Dummy summary: returning customer.</textarea>
      <button type="submit">enroll latest crop</button>
    </form>
  </section>
  <section>
    <h3>recognition</h3><ul>{recog_items}</ul><p>seq={recog_seq} age={_fmt_age(recog_ts)}</p>
    <h3>milvus</h3><ul>{diag_items}</ul>
    <h3>embedding projection</h3>
    {embedding_svg}
    <p class="muted">PCA debug projection of Milvus Lite vectors; distance here is approximate for inspection.</p>
    <table><tr><th>embedding</th><th>customer_id</th><th>name</th><th>model</th><th>quality</th><th>norm</th><th>created</th></tr>{embedding_rows}</table>
    <h3>customers</h3><table><tr><th>id</th><th>name</th><th>memory_card</th><th>updated</th></tr>{rows}</table>
    <h3>recent events</h3><table><tr><th>time</th><th>status</th><th>candidate</th><th>similarity</th><th>margin</th></tr>{event_rows}</table>
  </section>
</main>
</body></html>"""
    return HTMLResponse(body)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _short(value: object) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= 14 else f"{text[:8]}...{text[-4:]}"


def _fmt(value: object) -> str:
    return "" if value is None else f"{float(value):.3f}"


def _fmt_age(ts: float) -> str:
    if not ts:
        return "n/a"
    import time

    return f"{time.monotonic() - ts:.1f}s"


def _embedding_points() -> list[dict]:
    try:
        return get_face_service().embedding_projection(limit=200)
    except Exception:
        return []


def _render_embedding_svg(points: list[dict], customer_names: dict[str, str]) -> str:
    if not points:
        return "<p>no embeddings yet</p>"

    width, height, pad = 620, 300, 36
    xs = [float(p.get("x", 0.0)) for p in points]
    ys = [float(p.get("y", 0.0)) for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)

    def sx(x: float) -> float:
        return pad + ((x - min_x) / span_x) * (width - pad * 2)

    def sy(y: float) -> float:
        return height - pad - ((y - min_y) / span_y) * (height - pad * 2)

    circles = []
    for p in points:
        customer_id = str(p.get("customer_id") or "")
        name = customer_names.get(customer_id) or customer_id or "unknown"
        x = sx(float(p.get("x", 0.0)))
        y = sy(float(p.get("y", 0.0)))
        label = _e(name)
        title = _e(f"{name} {_fmt(p.get('quality'))}")
        circles.append(
            f"<g><title>{title}</title><circle cx='{x:.1f}' cy='{y:.1f}' r='6' fill='#0f0'/>"
            f"<text x='{x + 9:.1f}' y='{y - 8:.1f}' fill='#9f9' font-size='11'>{label}</text></g>"
        )
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Milvus embedding projection'>"
        "<line x1='36' y1='264' x2='584' y2='264' stroke='#063'/>"
        "<line x1='36' y1='36' x2='36' y2='264' stroke='#063'/>"
        + "".join(circles)
        + "</svg>"
    )
