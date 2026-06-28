"""SQLite source-of-truth storage for ShiftMind face recognition."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SQLITE_PATH = DATA_DIR / "shiftmind.sqlite3"
FACES_DIR = DATA_DIR / "faces"
AUDIO_DIR = DATA_DIR / "audio"


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    memory_card: str
    preferences: list[str]
    created_at: str
    updated_at: str


class Storage:
    def __init__(self, path: Path = SQLITE_PATH) -> None:
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    memory_card TEXT NOT NULL DEFAULT '',
                    preferences_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS face_images (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL REFERENCES customers(id),
                    path TEXT NOT NULL,
                    quality REAL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recognition_events (
                    id TEXT PRIMARY KEY,
                    candidate_customer_id TEXT,
                    status TEXT NOT NULL,
                    similarity REAL,
                    margin REAL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS customer_sessions (
                    id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL REFERENCES customers(id),
                    status TEXT NOT NULL,
                    is_new_customer INTEGER NOT NULL DEFAULT 0,
                    transcript TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    important_points_json TEXT NOT NULL DEFAULT '[]',
                    audio_path TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS locations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS employees (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pin_hash TEXT,
                    home_location_id TEXT REFERENCES locations(id),
                    created_at TEXT NOT NULL
                );
                """
            )
        self._migrate_add_employee_location_columns()
        self._migrate_add_qa_columns()
        self._migrate_add_is_simulated()

    def _migrate_add_qa_columns(self) -> None:
        """Add Q&A-agent columns to customer_sessions.

        Idempotent (PRAGMA table_info check) and additive only. Enables
        business-insight SQL (longest conversations, conversion rate, popular
        topics) without rewriting the session-finish flow. `topics_json`
        defaults to '[]' so it's always non-null and json_each-able; outcome
        and duration_seconds stay NULL until the summarizer/backfill fills them.
        """
        adds = {
            "customer_sessions": [
                ("topics_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("outcome", "TEXT"),
                ("duration_seconds", "REAL"),
            ],
        }
        with self.connect() as conn:
            for table, cols in adds.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_name, col_def in cols:
                    if col_name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

    def _migrate_add_is_simulated(self) -> None:
        """Add is_simulated flag to customers/employees/customer_sessions.

        Idempotent + additive. Marks rows seeded by the simulation/dev dataset
        (id prefix `sim-`) so the Q&A agent can distinguish demo data from real
        production data captured by the glasses. Default 0 = real.
        """
        adds = {
            "customers": [("is_simulated", "INTEGER NOT NULL DEFAULT 0")],
            "employees": [("is_simulated", "INTEGER NOT NULL DEFAULT 0")],
            "customer_sessions": [("is_simulated", "INTEGER NOT NULL DEFAULT 0")],
        }
        with self.connect() as conn:
            for table, cols in adds.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_name, col_def in cols:
                    if col_name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

    def _migrate_add_employee_location_columns(self) -> None:
        """Add employee_id/location_id to customer_sessions + recognition_events.

        Idempotent: checks PRAGMA table_info so existing DBs migrate on the next
        startup without re-running. Additive only (SQLite ALTER TABLE ADD COLUMN).
        """
        adds = {
            "customer_sessions": [("employee_id", "TEXT REFERENCES employees(id)"),
                                  ("location_id", "TEXT REFERENCES locations(id)")],
            "recognition_events": [("employee_id", "TEXT"), ("location_id", "TEXT")],
        }
        with self.connect() as conn:
            for table, cols in adds.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col_name, col_def in cols:
                    if col_name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

    def upsert_customer(
        self,
        *,
        customer_id: str,
        name: str,
        memory_card: str,
        preferences: list[str] | None = None,
    ) -> Customer:
        now = _now_iso()
        prefs = json.dumps(preferences or [], ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO customers (id, name, memory_card, preferences_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    memory_card=excluded.memory_card,
                    preferences_json=excluded.preferences_json,
                    updated_at=excluded.updated_at
                """,
                (customer_id, name, memory_card, prefs, now, now),
            )
        return self.get_customer(customer_id) or Customer(customer_id, name, memory_card, preferences or [], now, now)

    def get_customer(self, customer_id: str) -> Customer | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return _row_to_customer(row) if row else None

    def list_customers(self) -> list[Customer]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM customers ORDER BY updated_at DESC").fetchall()
        return [_row_to_customer(row) for row in rows]

    def next_customer_id(self, *, prefix: str = "person") -> str:
        pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM customers WHERE id LIKE ?", (f"{prefix}-%",)).fetchall()
        max_id = 0
        for row in rows:
            match = pattern.match(str(row["id"]))
            if match:
                max_id = max(max_id, int(match.group(1)))
        return f"{prefix}-{max_id + 1:03d}"

    def add_face_image(self, *, customer_id: str, image_bytes: bytes, quality: float | None) -> tuple[str, str]:
        image_id = f"face-{uuid.uuid4().hex}"
        path = FACES_DIR / f"{image_id}.jpg"
        path.write_bytes(image_bytes)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO face_images (id, customer_id, path, quality, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (image_id, customer_id, str(path.relative_to(BASE_DIR)), quality, _now_iso()),
            )
        return image_id, str(path)

    def add_recognition_event(
        self,
        *,
        status: str,
        candidate_customer_id: str | None,
        similarity: float | None,
        margin: float | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO recognition_events (id, candidate_customer_id, status, similarity, margin, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"event-{uuid.uuid4().hex}", candidate_customer_id, status, similarity, margin, _now_iso()),
            )

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*, c.name AS customer_name
                FROM recognition_events e
                LEFT JOIN customers c ON c.id = e.candidate_customer_id
                ORDER BY e.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_customer_session(
        self,
        *,
        customer_id: str,
        is_new_customer: bool,
        employee_id: str | None = None,
        location_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = f"session-{uuid.uuid4().hex}"
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO customer_sessions
                    (id, customer_id, status, is_new_customer, employee_id, location_id, started_at)
                VALUES (?, ?, 'active', ?, ?, ?, ?)
                """,
                (session_id, customer_id, 1 if is_new_customer else 0, employee_id, location_id, now),
            )
        return self.get_customer_session(session_id) or {
            "id": session_id,
            "customer_id": customer_id,
            "status": "active",
            "is_new_customer": 1 if is_new_customer else 0,
            "employee_id": employee_id,
            "location_id": location_id,
            "started_at": now,
        }

    def get_customer_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, c.name AS customer_name, c.memory_card
                FROM customer_sessions s
                JOIN customers c ON c.id = s.customer_id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_active_session(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, c.name AS customer_name, c.memory_card
                FROM customer_sessions s
                JOIN customers c ON c.id = s.customer_id
                WHERE s.status = 'active'
                ORDER BY s.started_at DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def abort_active_sessions(self) -> int:
        """Mark any lingering 'active' sessions as 'aborted'.

        Called on server startup so a session that never received a finish
        (server killed mid-interaction) does not get resurrected on the next
        frame and falsely re-confirm the last customer.
        """
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE customer_sessions SET status = 'aborted', ended_at = ? "
                "WHERE status = 'active'",
                (_now_iso(),),
            )
            return cur.rowcount


    def finish_customer_session(
        self,
        *,
        session_id: str,
        transcript: str,
        summary: str,
        important_points: list[str],
        audio_bytes: bytes | None,
        topics: list[str] | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_customer_session(session_id)
        if not session:
            raise KeyError(session_id)
        audio_path = None
        if audio_bytes:
            path = AUDIO_DIR / f"{session_id}.wav"
            path.write_bytes(audio_bytes)
            audio_path = str(path.relative_to(BASE_DIR))
        ended_at = _now_iso()
        duration_seconds = _duration_seconds(str(session.get("started_at") or ""), ended_at)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE customer_sessions
                SET status = 'ended',
                    transcript = ?,
                    summary = ?,
                    important_points_json = ?,
                    audio_path = COALESCE(?, audio_path),
                    ended_at = ?,
                    topics_json = ?,
                    outcome = ?,
                    duration_seconds = ?
                WHERE id = ?
                """,
                (
                    transcript,
                    summary,
                    json.dumps(important_points, ensure_ascii=False),
                    audio_path,
                    ended_at,
                    json.dumps(topics or [], ensure_ascii=False),
                    outcome,
                    duration_seconds,
                    session_id,
                ),
            )
        return self.get_customer_session(session_id) or {}

    def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, c.name AS customer_name
                FROM customer_sessions s
                JOIN customers c ON c.id = s.customer_id
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_customer_session_history(self, customer_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """Recent ended sessions for a customer, enriched with employee + location
        (for the memory-card rewrite prompt's cross-employee continuity)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.summary, s.important_points_json, s.started_at, s.ended_at,
                       e.name AS employee_name, l.name AS location_name
                FROM customer_sessions s
                LEFT JOIN employees e ON e.id = s.employee_id
                LEFT JOIN locations l ON l.id = s.location_id
                WHERE s.customer_id = ? AND s.status = 'ended' AND s.summary != ''
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (customer_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_last_interaction_by_customer(
        self, customer_id: str, *, exclude_session_id: str | None = None
    ) -> dict[str, Any] | None:
        """Most recent ended session with an attributed employee — drives the
        summary prompt's "last employee who interacted" line. None if never seen
        by a logged-in employee."""
        query = (
            "SELECT s.ended_at, e.name AS employee_name, l.name AS location_name "
            "FROM customer_sessions s "
            "LEFT JOIN employees e ON e.id = s.employee_id "
            "LEFT JOIN locations l ON l.id = s.location_id "
            "WHERE s.customer_id = ? AND s.status = 'ended' AND s.employee_id IS NOT NULL "
        )
        params: list[Any] = [customer_id]
        if exclude_session_id:
            query += "AND s.id != ? "
            params.append(exclude_session_id)
        query += "ORDER BY s.ended_at DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None

    # ── employees + locations (shared-memory multi-tenant) ────────────────

    def list_locations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, created_at FROM locations ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_employees(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT e.id, e.name, e.home_location_id, l.name AS location_name, e.created_at "
                "FROM employees e LEFT JOIN locations l ON l.id = e.home_location_id "
                "ORDER BY e.created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_employee(self, employee_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT e.id, e.name, e.pin_hash, e.home_location_id, l.name AS location_name "
                "FROM employees e LEFT JOIN locations l ON l.id = e.home_location_id "
                "WHERE e.id = ?",
                (employee_id,),
            ).fetchone()
        return dict(row) if row else None

    def next_location_id(self, *, prefix: str = "loc") -> str:
        return _next_sequential_id(self, "locations", prefix)

    def next_employee_id(self, *, prefix: str = "emp") -> str:
        return _next_sequential_id(self, "employees", prefix)

    def create_location(self, *, name: str) -> dict[str, Any]:
        location_id = self.next_location_id()
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO locations (id, name, created_at) VALUES (?, ?, ?)",
                (location_id, name.strip(), now),
            )
        return {"id": location_id, "name": name.strip(), "created_at": now}

    def create_employee(
        self,
        *,
        name: str,
        pin_hash: str | None = None,
        home_location_id: str | None = None,
    ) -> dict[str, Any]:
        employee_id = self.next_employee_id()
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO employees (id, name, pin_hash, home_location_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (employee_id, name.strip(), pin_hash, home_location_id, now),
            )
        return {
            "id": employee_id,
            "name": name.strip(),
            "pin_hash": pin_hash,
            "home_location_id": home_location_id,
            "created_at": now,
        }


def _next_sequential_id(storage: "Storage", table: str, prefix: str) -> str:
    """loc-001 / emp-001 / ... style ids, mirroring next_customer_id's scheme."""
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    with storage.connect() as conn:
        rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ?", (f"{prefix}-%",)).fetchall()
    max_id = 0
    for row in rows:
        match = pattern.match(str(row["id"]))
        if match:
            max_id = max(max_id, int(match.group(1)))
    return f"{prefix}-{max_id + 1:03d}"


def _row_to_customer(row: sqlite3.Row) -> Customer:
    return Customer(
        id=row["id"],
        name=row["name"],
        memory_card=row["memory_card"],
        preferences=json.loads(row["preferences_json"] or "[]"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _duration_seconds(started_iso: str, ended_iso: str) -> float | None:
    """Seconds between two _now_iso() timestamps, or None if either is unparseable.

    Stored on customer_sessions at finish so the Q&A agent can rank/aggregate
    conversation length with plain SQL instead of re-parsing timestamps.
    """
    try:
        start = time.mktime(time.strptime(started_iso, _ISO_FMT))
        end = time.mktime(time.strptime(ended_iso, _ISO_FMT))
        return max(0.0, end - start)
    except (ValueError, TypeError):
        return None


storage = Storage()
