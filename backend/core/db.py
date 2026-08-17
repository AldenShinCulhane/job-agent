"""SQLite persistence. Single file in `.data/`, restrictive permissions.

M0 scope: durable engine state (pinned model, usage_exhausted wait state)
that survives restarts. Later milestones add profile versions, jobs,
scores, and the package state machine on the same database.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
DB_PATH = DATA_DIR / "job_agent.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_versions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_json TEXT NOT NULL,
    confirmed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""


def init() -> None:
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # mkdir mode is subject to umask; enforce
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    os.chmod(DB_PATH, 0o600)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_state(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM engine_state WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_state(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO engine_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )


def delete_state(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM engine_state WHERE key = ?", (key,))


# --- Profile versions (append-only; tailoring pins a specific version) ------

def save_profile(profile: dict) -> int:
    import json

    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO profile_versions (profile_json, created_at) VALUES (?, ?)",
            (json.dumps(profile), now),
        )
        return cursor.lastrowid


def latest_profile() -> tuple[int, dict, bool] | None:
    """(version, profile, confirmed) for the newest version, or None."""
    import json

    with _connect() as conn:
        row = conn.execute(
            "SELECT version, profile_json, confirmed FROM profile_versions "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return row[0], json.loads(row[1]), bool(row[2])


def get_profile(version: int) -> dict | None:
    import json

    with _connect() as conn:
        row = conn.execute(
            "SELECT profile_json FROM profile_versions WHERE version = ?", (version,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def confirm_profile(version: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE profile_versions SET confirmed = 1 WHERE version = ?", (version,)
        )
