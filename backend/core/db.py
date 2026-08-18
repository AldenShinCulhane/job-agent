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
CREATE TABLE IF NOT EXISTS artifacts (
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL,
    engine     TEXT NOT NULL,
    pages      INTEGER NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, ref)
);
CREATE TABLE IF NOT EXISTS search_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    config_json TEXT NOT NULL,
    status      TEXT NOT NULL,
    next_start  INTEGER NOT NULL DEFAULT 0,
    collected   INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    company           TEXT NOT NULL,
    location          TEXT,
    posted_text       TEXT,
    workplace         TEXT,
    easy_apply        INTEGER NOT NULL DEFAULT 0,
    url               TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    description       TEXT,
    description_hash  TEXT,
    detail_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS search_run_jobs (
    run_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id)
);
CREATE TABLE IF NOT EXISTS action_counts (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL
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


# --- Artifact provenance (which engine produced a PDF, and its page count;
# page-limit decisions are only valid for the engine that made them) ---------

def record_artifact(kind: str, ref: str, engine: str, pages: int, path: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO artifacts (kind, ref, engine, pages, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, ref) DO UPDATE SET engine = excluded.engine, "
            "pages = excluded.pages, path = excluded.path, "
            "created_at = excluded.created_at",
            (kind, ref, engine, pages, path, now),
        )


def get_artifact(kind: str, ref: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT engine, pages, path, created_at FROM artifacts "
            "WHERE kind = ? AND ref = ?",
            (kind, ref),
        ).fetchone()
    if row is None:
        return None
    return {"engine": row[0], "pages": row[1], "path": row[2], "created_at": row[3]}


# --- Jobs (deduped by LinkedIn job id; detail cached with TTL + hash) -------

_JOB_COLUMNS = (
    "job_id", "title", "company", "location", "posted_text", "workplace",
    "easy_apply", "url", "first_seen_at", "last_seen_at",
    "description", "description_hash", "detail_fetched_at",
)


def _job_row_to_dict(row) -> dict:
    job = dict(zip(_JOB_COLUMNS, row))
    job["easy_apply"] = bool(job["easy_apply"])
    return job


def upsert_job(card: dict) -> bool:
    """Insert a search-result card, or refresh its card fields if the job
    is already known. Returns True when the job was new (for dedupe/cap
    accounting). Never touches cached detail columns."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (card["job_id"],)
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO jobs (job_id, title, company, location, posted_text, "
            "workplace, easy_apply, url, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET title = excluded.title, "
            "company = excluded.company, location = excluded.location, "
            "posted_text = excluded.posted_text, workplace = excluded.workplace, "
            "easy_apply = excluded.easy_apply, url = excluded.url, "
            "last_seen_at = excluded.last_seen_at",
            (
                card["job_id"], card["title"], card["company"],
                card.get("location"), card.get("posted_text"),
                card.get("workplace"), int(card.get("easy_apply", False)),
                card["url"], now, now,
            ),
        )
    return not existed


def get_job(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _job_row_to_dict(row) if row else None


def list_jobs(run_id: int | None = None) -> list[dict]:
    """All cached jobs, or — with `run_id` — only the jobs that belong to
    that search run (what the UI shows as the current results)."""
    columns = ", ".join(f"jobs.{c}" for c in _JOB_COLUMNS)
    with _connect() as conn:
        if run_id is None:
            rows = conn.execute(
                f"SELECT {columns} FROM jobs ORDER BY last_seen_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns} FROM jobs "
                "JOIN search_run_jobs ON search_run_jobs.job_id = jobs.job_id "
                "WHERE search_run_jobs.run_id = ? ORDER BY jobs.last_seen_at DESC",
                (run_id,),
            ).fetchall()
    return [_job_row_to_dict(r) for r in rows]


def record_search_page(run_id: int, cards: list[dict], next_start: int, cap: int) -> int:
    """Persist one results page atomically: upsert the cards into the
    global job cache, associate them with the run (up to `cap` members),
    and checkpoint the run's cursor + collected count — all in a single
    transaction, so a crash between card insertion and checkpoint can't
    desync the accounting. Idempotent: re-recording the same page after
    a crash changes nothing. Returns the run's collected total."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        for card in cards:
            collected = conn.execute(
                "SELECT COUNT(*) FROM search_run_jobs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if collected >= cap:
                break
            conn.execute(
                "INSERT INTO jobs (job_id, title, company, location, posted_text, "
                "workplace, easy_apply, url, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET title = excluded.title, "
                "company = excluded.company, location = excluded.location, "
                "posted_text = excluded.posted_text, workplace = excluded.workplace, "
                "easy_apply = excluded.easy_apply, url = excluded.url, "
                "last_seen_at = excluded.last_seen_at",
                (
                    card["job_id"], card["title"], card["company"],
                    card.get("location"), card.get("posted_text"),
                    card.get("workplace"), int(card.get("easy_apply", False)),
                    card["url"], now, now,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO search_run_jobs (run_id, job_id) VALUES (?, ?)",
                (run_id, card["job_id"]),
            )
        collected = conn.execute(
            "SELECT COUNT(*) FROM search_run_jobs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE search_runs SET next_start = ?, collected = ?, updated_at = ? "
            "WHERE id = ?",
            (next_start, collected, now, run_id),
        )
    return collected


def save_job_detail(job_id: str, description: str, description_hash: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET description = ?, description_hash = ?, "
            "detail_fetched_at = ? WHERE job_id = ?",
            (description, description_hash, now, job_id),
        )


# --- Search runs (resumable across restarts) --------------------------------

def create_search_run(config: dict) -> int:
    import json

    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO search_runs (config_json, status, started_at, updated_at) "
            "VALUES (?, 'running', ?, ?)",
            (json.dumps(config), now, now),
        )
        return cursor.lastrowid


_UNSET = object()


def update_search_run(
    run_id: int,
    *,
    status: str | None = None,
    next_start: int | None = None,
    collected: int | None = None,
    message: object = _UNSET,
) -> None:
    """`message` uses a sentinel so passing message=None explicitly
    *clears* a stale error message (omit the argument to leave it)."""
    sets, params = ["updated_at = ?"], [datetime.now(timezone.utc).isoformat()]
    for column, value in (
        ("status", status), ("next_start", next_start), ("collected", collected),
    ):
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    if message is not _UNSET:
        sets.append("message = ?")
        params.append(message)
    params.append(run_id)
    with _connect() as conn:
        conn.execute(f"UPDATE search_runs SET {', '.join(sets)} WHERE id = ?", params)


def get_search_run(run_id: int) -> dict | None:
    return _search_run_query("WHERE id = ?", (run_id,))


def latest_search_run() -> dict | None:
    return _search_run_query("ORDER BY id DESC LIMIT 1", ())


def running_search_runs() -> list[dict]:
    """Runs left in 'running' — after a restart these are orphans (no
    task holds them) and must be reclaimed by startup recovery."""
    import json

    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, config_json, status, next_start, collected, message, "
            "started_at, updated_at FROM search_runs WHERE status = 'running'"
        ).fetchall()
    return [
        {
            "id": r[0], "config": json.loads(r[1]), "status": r[2],
            "next_start": r[3], "collected": r[4], "message": r[5],
            "started_at": r[6], "updated_at": r[7],
        }
        for r in rows
    ]


def _search_run_query(clause: str, params: tuple) -> dict | None:
    import json

    with _connect() as conn:
        row = conn.execute(
            "SELECT id, config_json, status, next_start, collected, message, "
            f"started_at, updated_at FROM search_runs {clause}",
            params,
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "config": json.loads(row[1]), "status": row[2],
        "next_start": row[3], "collected": row[4], "message": row[5],
        "started_at": row[6], "updated_at": row[7],
    }


# --- Daily action counter (pacing cap) --------------------------------------

def bump_action_count(day: str, by: int = 1) -> int:
    """Increment today's browser-action counter, returning the new total."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO action_counts (day, count) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET count = count + excluded.count",
            (day, by),
        )
        return conn.execute(
            "SELECT count FROM action_counts WHERE day = ?", (day,)
        ).fetchone()[0]


def get_action_count(day: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM action_counts WHERE day = ?", (day,)
        ).fetchone()
    return row[0] if row else 0
