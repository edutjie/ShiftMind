"""Remove dev/test-fixture rows that polluted the real DB's dropdowns.

Earlier versions of tests/test_multiemployee.py wrote test-emp-a/b (single-token
"Maya"/"Joan"), test-loc-a/b ("Site A"/"Site B"), person-test-*, person-verify,
and stray test sessions directly into data/shiftmind.sqlite3. The tests are now
isolated (tmp DB), but the leftover rows remain. This script removes them so the
Q&A dropdowns show only realistic (sim + real) data.

Idempotent + safe: only deletes id-prefixed test rows. Re-runnable.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from storage import storage  # noqa: E402

# customer_ids that are test fixtures (their sessions cascade via FK).
TEST_CUSTOMERS = ("person-test-%", "person-verify")
# session ids used by tests (id prefixes).
TEST_SESSION_PREFIXES = ("li-", "ln-", "stamp-", "nologin-")


def clean() -> dict[str, int]:
    counts: dict[str, int] = {}
    with storage.connect() as c:
        # sessions on test customers + test-prefixed sessions
        c.execute("DELETE FROM customer_sessions WHERE customer_id LIKE 'person-test-%'")
        c.execute("DELETE FROM customer_sessions WHERE customer_id = 'person-verify'")
        for prefix in TEST_SESSION_PREFIXES:
            c.execute("DELETE FROM customer_sessions WHERE id LIKE ?", (f"{prefix}%",))
        c.execute("DELETE FROM customer_sessions WHERE id LIKE 'test-%'")
        counts["sessions"] = c.total_changes

        c.execute("DELETE FROM face_images WHERE customer_id LIKE 'person-test-%'")
        c.execute("DELETE FROM face_images WHERE customer_id = 'person-verify'")
        c.execute("DELETE FROM customers WHERE id LIKE 'person-test-%'")
        c.execute("DELETE FROM customers WHERE id = 'person-verify'")
        c.execute("DELETE FROM employees WHERE id LIKE 'test-%'")
        c.execute("DELETE FROM locations WHERE id LIKE 'test-%'")
        c.commit()
        counts["rows"] = c.total_changes
    return counts


def main() -> None:
    print("[clean] removing test-fixture rows …")
    counts = clean()
    print(f"[clean] done: {counts}")
    with storage.connect() as c:
        print("  remaining employees:", [r["name"] for r in c.execute("SELECT name FROM employees ORDER BY name")])
        print("  remaining locations:", [r["name"] for r in c.execute("SELECT name FROM locations ORDER BY name")])


if __name__ == "__main__":
    main()
