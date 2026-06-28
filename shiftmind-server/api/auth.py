"""Employee/location login + admin (dev seed) for shared-memory ShiftMind.

Login is light: pick an employee (+ optional PIN) and a location on a web page
opened from a phone/laptop; the server marks them the active worker for the
shift. The glasses read the active login via the preview-frame response and
stamp it onto every customer session.

Follows the inline-HTMLResponse pattern of api/faces.py and api/preview.py.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import active_login, hash_pin, verify_pin
from storage import storage

router = APIRouter()


@router.get("/api/whoami")
async def whoami() -> dict:
    """Active employee/location, or {active: false}. Glasses consume this via
    the preview-frame response; this endpoint is for the login page + debugging."""
    return active_login.to_dict()


@router.post("/api/login")
async def login(
    employee_id: str = Form(...),
    location_id: str = Form(default=""),
    pin: str = Form(default=""),
) -> RedirectResponse:
    employee = storage.get_employee(employee_id.strip())
    if not employee:
        raise HTTPException(status_code=404, detail="unknown employee")
    if not verify_pin(pin, employee.get("pin_hash")):
        raise HTTPException(status_code=401, detail="invalid pin")
    # Fall back to the employee's home location if none chosen.
    loc_id = location_id.strip() or employee.get("home_location_id")
    loc_name = None
    if loc_id:
        loc = next((l for l in storage.list_locations() if l["id"] == loc_id), None)
        loc_name = loc["name"] if loc else None
    active_login.set(
        employee_id=employee["id"],
        employee_name=employee["name"],
        location_id=loc_id,
        location_name=loc_name,
    )
    return RedirectResponse(url="/api/login.html", status_code=303)


@router.post("/api/logout")
async def logout() -> RedirectResponse:
    active_login.clear()
    return RedirectResponse(url="/api/login.html", status_code=303)


@router.post("/api/admin/location")
async def admin_create_location(name: str = Form(...)) -> RedirectResponse:
    name = name.strip()
    if name:
        storage.create_location(name=name)
    return RedirectResponse(url="/api/login.html", status_code=303)


@router.post("/api/admin/employee")
async def admin_create_employee(
    name: str = Form(...),
    home_location_id: str = Form(default=""),
    pin: str = Form(default=""),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        return RedirectResponse(url="/api/login.html", status_code=303)
    pin_hash = hash_pin(pin) if pin.strip() else None
    storage.create_employee(
        name=name,
        pin_hash=pin_hash,
        home_location_id=home_location_id.strip() or None,
    )
    return RedirectResponse(url="/api/login.html", status_code=303)


@router.get("/api/login.html")
async def login_html() -> HTMLResponse:
    current = active_login.to_dict()
    locations = storage.list_locations()
    employees = storage.list_employees()

    loc_options = "".join(
        f"<option value='{_e(l['id'])}'>{_e(l['id'])} — {_e(l['name'])}</option>"
        for l in locations
    ) or "<option value=''>(no locations — create one below)</option>"
    emp_options = "".join(
        f"<option value='{_e(e['id'])}'>{_e(e['name'])}"
        + (f" · {_e(e['location_name'])}" if e.get("location_name") else "")
        + ("" if e.get("pin_hash") else " · no pin")
        + "</option>"
        for e in employees
    ) or "<option value=''>(no employees — create one below)</option>"

    if current.get("active"):
        status = (
            f"<p class='ok'>Logged in: <b>{_e(current['employee_name'])}</b>"
            f" @ <b>{_e(current.get('location_name') or '—')}</b></p>"
            "<form action='/api/logout' method='post'><button type='submit'>Log out</button></form>"
        )
    else:
        status = "<p class='muted'>Not logged in — pick an employee to start a shift.</p>"

    body = f"""<!doctype html>
<html><head><title>ShiftMind login</title>
<style>
body{{background:#050505;color:#0f0;font:14px monospace;margin:0;padding:18px;max-width:760px}}
a{{color:#9f9}} section{{border:1px solid #0f0;padding:12px;margin-top:14px}}
input,select,button{{background:#000;color:#0f0;border:1px solid #0f0;font:14px monospace;padding:6px;width:100%;box-sizing:border-box}}
button{{cursor:pointer;margin-top:6px}} label{{display:block;margin-top:8px;color:#8f8}}
.ok{{color:#0f0}} .muted{{color:#8f8}}
</style></head>
<body>
<h2>ShiftMind — employee login</h2>
<p><a href="/api/preview.html">preview</a> · <a href="/api/faces.html">faces</a> · <a href="/api/whoami">whoami json</a></p>
<section>
  <h3>Current shift</h3>
  {status}
</section>
<section>
  <h3>Log in to a shift</h3>
  <form action="/api/login" method="post">
    <label>employee</label>
    <select name="employee_id" required>{emp_options}</select>
    <label>location</label>
    <select name="location_id"><option value="">(employee home)</option>{loc_options}</select>
    <label>pin (optional — leave blank if none)</label>
    <input name="pin" type="password" autocomplete="off"/>
    <button type="submit">Start shift</button>
  </form>
</section>
<section>
  <h3>Admin — seed locations &amp; employees (dev)</h3>
  <form action="/api/admin/location" method="post">
    <label>new location name</label>
    <input name="name" placeholder="Downtown flagship" required/>
    <button type="submit">Add location</button>
  </form>
  <form action="/api/admin/employee" method="post" style="margin-top:12px">
    <label>new employee name</label>
    <input name="name" placeholder="Maya Chen" required/>
    <label>home location</label>
    <select name="home_location_id"><option value="">(none)</option>{loc_options}</select>
    <label>pin (optional)</label>
    <input name="pin" type="password" autocomplete="off"/>
    <button type="submit">Add employee</button>
  </form>
  <p class='muted'>No auth gate on these — dev seed only, consistent with /api/enroll-face.</p>
</section>
</body></html>"""
    return HTMLResponse(body)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))
