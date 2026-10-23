"""SQLite persistence. Single file in `.data/`, restrictive permissions.

M0 scope: durable engine state (pinned model, usage_exhausted wait state)
that survives restarts. Later milestones add profile versions, jobs,
scores, and the package state machine on the same database.
"""

import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from backend.core import duplicates

DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".data"
DB_PATH = DATA_DIR / "job_agent.sqlite3"

# The packages table stands alone because the migration below must
# create it inside a transaction, where `executescript()` cannot go.
_PACKAGES_TABLE = """
CREATE TABLE IF NOT EXISTS packages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                TEXT NOT NULL,
    profile_version       INTEGER NOT NULL,
    jd_hash               TEXT NOT NULL,
    page_limit            INTEGER NOT NULL DEFAULT 1,
    template_version      INTEGER NOT NULL DEFAULT 1,
    cover_template_version INTEGER NOT NULL DEFAULT 1,
    attempt               INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL DEFAULT 'queued',
    stage_json            TEXT,
    error                 TEXT,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    provenance_json       TEXT,
    output_dir            TEXT,
    retry_of              INTEGER,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (job_id, profile_version, jd_hash, page_limit,
            template_version, cover_template_version, attempt)
);
"""

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
CREATE TABLE IF NOT EXISTS requirements_cache (
    jd_hash           TEXT PRIMARY KEY,
    requirements_json TEXT NOT NULL,
    normalized_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_scores (
    job_id              TEXT PRIMARY KEY,
    jd_hash             TEXT NOT NULL,
    profile_version     INTEGER NOT NULL,
    deterministic_score REAL NOT NULL,
    breakdown_json      TEXT NOT NULL,
    llm_rerank          REAL,
    llm_rank            INTEGER,
    explanation_json    TEXT,
    scored_at           TEXT NOT NULL
);
-- Which postings repeat one role (PLAN.md P8a): derived from the jobs
-- table, one row per posting that can be grouped. match_key is the
-- bucket it was grouped in, so a change of company, title or location
-- knows which old bucket to recompute; group_id is the group's first
-- member, never referenced from anywhere else.
CREATE TABLE IF NOT EXISTS posting_groups (
    job_id       TEXT PRIMARY KEY,
    match_key    TEXT NOT NULL,
    group_id     TEXT NOT NULL,
    rule_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS posting_groups_by_key ON posting_groups (match_key);
CREATE INDEX IF NOT EXISTS posting_groups_by_group ON posting_groups (group_id);
CREATE TABLE IF NOT EXISTS package_requests (
    request_key TEXT PRIMARY KEY,
    source_id   INTEGER NOT NULL,
    package_id  INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
-- The user's application record (PLAN.md P8b): append-only, one row per
-- status the user set on a posting, never edited or deleted. A posting's
-- state is its event with the highest id; occurred_on is the day the
-- user gives, reported but never the order. Keyed by the posting itself,
-- so a change of groups around it loses nothing.
CREATE TABLE IF NOT EXISTS application_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    package_id  INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('applied', 'interviewing',
                    'offer', 'rejected', 'withdrawn', 'cleared')),
    occurred_on TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS application_events_by_job
    ON application_events (job_id, id);
""" + _PACKAGES_TABLE

# One-shot schema fixes for databases created by earlier revisions of
# this branch (executescript's IF NOT EXISTS cannot alter shapes).
_MIGRATIONS = (
    # requirements were briefly keyed per-job; the cache is now
    # content-addressed by jd_hash (cheap to regenerate — just drop).
    "DROP TABLE IF EXISTS job_requirements",
    # rerank rank ordinal added after the table first shipped.
    "ALTER TABLE job_scores ADD COLUMN llm_rank INTEGER",
)


# Named so a test can replace it with a statement that fails, and prove
# the migration rolls back whole rather than half.
_COPY_PACKAGES_SQL = (
    "INSERT INTO packages (id, job_id, profile_version, jd_hash, "
    "page_limit, template_version, cover_template_version, status, "
    "stage_json, error, retry_count, provenance_json, output_dir, "
    "created_at, updated_at) "
    "SELECT id, job_id, profile_version, jd_hash, page_limit, "
    "COALESCE(json_extract(provenance_json, '$.template_version'), 1), "
    "COALESCE(json_extract(provenance_json, '$.cover_template_version'), 1), "
    "status, stage_json, error, retry_count, provenance_json, "
    "output_dir, created_at, updated_at FROM packages_pre_tpl_version"
)


# The attempt number joined package identity after packages had been
# published (PLAN.md §4.5a). Every existing row is attempt 1 of its
# inputs; the row a retry came from is new, so it starts empty. Named
# for the same reason as the copy above.
_COPY_PACKAGES_PRE_ATTEMPT_SQL = (
    "INSERT INTO packages (id, job_id, profile_version, jd_hash, "
    "page_limit, template_version, cover_template_version, attempt, "
    "status, stage_json, error, retry_count, provenance_json, output_dir, "
    "created_at, updated_at) "
    "SELECT id, job_id, profile_version, jd_hash, page_limit, "
    "template_version, cover_template_version, 1, status, stage_json, "
    "error, retry_count, provenance_json, output_dir, created_at, "
    "updated_at FROM packages_pre_attempt"
)


def _rebuild_packages(conn, old_name: str, copy_sql: str) -> None:
    """Rebuild `packages` in the current shape, keeping every row.

    SQLite cannot widen a UNIQUE constraint in place, and this table
    holds real packages — their PDFs are on disk under paths named by
    the row id, so every id must survive as it was. So: rename, recreate,
    copy across, all inside one transaction, and the copy is counted
    before the old table goes. Half a migration is worse than none: if
    the rename and the CREATE landed but the copy did not, the next
    startup would find the new column on an empty `packages`, skip the
    migration forever, and every published row — PDFs still on disk —
    would be gone from the app with no way back. That is why
    `executescript()` cannot appear here: it COMMITs whatever is open
    before it runs, which is precisely the split that leaves that state
    behind. A crash mid-way rolls back to the old table, and the next
    startup simply migrates again.

    The id counter moves across too. Ids name output directories, so one
    handed out before must never be handed out again, and a copy alone
    only carries the highest id still present.
    """
    conn.commit()  # nothing of ours rides along inside this transaction
    conn.execute("BEGIN IMMEDIATE")
    try:
        before = conn.execute("SELECT count(*) FROM packages").fetchone()[0]
        conn.execute(f"ALTER TABLE packages RENAME TO {old_name}")
        conn.execute(_PACKAGES_TABLE)
        conn.execute(copy_sql)
        after = conn.execute("SELECT count(*) FROM packages").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError(
                f"package migration copied {after} of {before} rows")
        seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?",
                           (old_name,)).fetchone()
        if seq is not None:
            conn.execute(
                "UPDATE sqlite_sequence SET seq = max(seq, ?) WHERE name = 'packages'",
                (seq[0],))
        conn.execute(f"DROP TABLE {old_name}")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _widen_package_identity(conn) -> None:
    """Add the template versions to package identity, keeping every row
    (one transaction, counted; see `_rebuild_packages`).

    Existing rows are pinned to the versions their own provenance
    records, or to 1 when they predate that record; either way they keep
    the identity they were built under, and a selection made under newer
    templates becomes a new package instead of silently returning the old
    documents. The table it builds is the current one, so these rows land
    as attempt 1 in the same step.
    """
    _rebuild_packages(conn, "packages_pre_tpl_version", _COPY_PACKAGES_SQL)


def _add_package_attempts(conn) -> None:
    """Add the attempt number to package identity (PLAN.md §4.5a): every
    existing row becomes attempt 1 of its inputs, with its id, status,
    stage and output directory — and so its downloads — unchanged."""
    _rebuild_packages(conn, "packages_pre_attempt", _COPY_PACKAGES_PRE_ATTEMPT_SQL)


# The rule a database's groups were built with. Groups are derived, so a
# rule change simply rebuilds them all.
_GROUPS_RULE_KEY = "posting_groups_rule"


def _rebuild_groups(conn) -> None:
    """Rebuild every posting group under the current rule, in one
    transaction: a crash or an error part-way leaves the groups exactly
    as they were, and the next start rebuilds again. Nothing else
    references a group, so a rebuild changes no other table."""
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM posting_groups")
        buckets: dict[str, list[dict]] = defaultdict(list)
        for posting in _grouping_rows(conn, None):
            key = duplicates.match_key(posting)
            if key is not None:
                buckets[key].append(posting)
        for key in sorted(buckets):
            _write_groups(conn, key, duplicates.group(buckets[key]))
        conn.execute(
            "INSERT INTO engine_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (_GROUPS_RULE_KEY, str(duplicates.RULE_VERSION),
             datetime.now(timezone.utc).isoformat()))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def init() -> None:
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # mkdir mode is subject to umask; enforce
    with _connect() as conn:
        # The page limit joined package identity (and its UNIQUE
        # constraint) after the table first shipped on this branch;
        # SQLite cannot widen a table constraint in place, and no
        # released build ever wrote package rows — rebuild.
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(packages)").fetchall()}
        if cols and "page_limit" not in cols:
            conn.execute("DROP TABLE packages")
            cols = set()
        conn.executescript(_SCHEMA)
        if cols and "template_version" not in cols:
            _widen_package_identity(conn)
        elif cols and "attempt" not in cols:
            _add_package_attempts(conn)
        for statement in _MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # already applied (e.g. duplicate column)
        rule = conn.execute("SELECT value FROM engine_state WHERE key = ?",
                            (_GROUPS_RULE_KEY,)).fetchone()
        if rule is None or rule[0] != str(duplicates.RULE_VERSION):
            _rebuild_groups(conn)
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


def live_profile_version() -> int | None:
    """The newest CONFIRMED version: what scoring and generation resume on,
    and what a discard falls back to.

    Distinct from `latest_profile()`, which returns the newest row of any
    kind: an upload or an edit inserts an unconfirmed row that becomes
    "latest" without becoming live. While such a draft exists the
    pipeline runs on nothing at all — it is paused, not running on this
    version.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT version FROM profile_versions WHERE confirmed = 1 "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else None


def discard_unconfirmed_above_live() -> tuple[int, list[int]] | None:
    """Drop every unconfirmed version above the live one; None when no
    live version exists to fall back to.

    Without this an unconfirmed row is a dead end: `latest_profile()`
    keeps returning it, so `confirmed_profile()` refuses and scoring and
    generation stay blocked with no way back. Deleting is safe precisely
    because unconfirmed versions can never be referenced — a package or a
    score can only be created against a confirmed profile.

    One transaction: reading the live version and deleting above it must
    not straddle a concurrent confirm, or the delete could take a row
    that just became live.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT version FROM profile_versions WHERE confirmed = 1 "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        live = row[0]
        doomed = [r[0] for r in conn.execute(
            "SELECT version FROM profile_versions "
            "WHERE version > ? AND confirmed = 0 ORDER BY version", (live,)
        )]
        if doomed:
            conn.execute(
                "DELETE FROM profile_versions WHERE version > ? AND confirmed = 0",
                (live,),
            )
        conn.commit()
        return live, doomed
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_profile(version: int) -> dict | None:
    import json

    with _connect() as conn:
        row = conn.execute(
            "SELECT profile_json FROM profile_versions WHERE version = ?", (version,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def confirm_profile(version: int) -> list[int] | None:
    """Confirm `version` and supersede packages built on anything else.

    Returns the superseded package ids, or None when the version is gone
    or is no longer the newest — the caller lost a race and must not
    report success.

    One BEGIN IMMEDIATE transaction, and the supersede is gated on the
    confirm actually landing. The route reads the profile and compiles a
    preview before calling this, which is seconds wide: a discard can
    delete the row in that window. The old code ignored the UPDATE's row
    count, so a confirm of a deleted version still ran the supersede —
    failing every unfinished package (none of which match the vanished
    version) and returning 200 for a version that no longer existed.

    "Newest" is also required, not just "exists": confirming a stale
    version would supersede the packages of a newer confirmed one.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT version FROM profile_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None or row[0] != version:
            conn.rollback()
            return None
        cursor = conn.execute(
            "UPDATE profile_versions SET confirmed = 1 WHERE version = ?", (version,)
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        # Unfinished packages pinned to any other profile version are now
        # stale inputs: without this they strand forever (job_scores only
        # keeps the current profile's row, so their waiting_inputs check
        # never passes) or publish against a profile the user replaced.
        # Done packages are history and stay untouched. Ids are read
        # first so the caller can announce exactly what changed.
        superseded = [r[0] for r in conn.execute(
            "SELECT id FROM packages WHERE profile_version != ? "
            "AND status NOT IN ('done', 'failed')", (version,)
        )]
        if superseded:
            conn.execute(
                "UPDATE packages SET status = 'failed', error = ?, updated_at = ? "
                "WHERE profile_version != ? AND status NOT IN ('done', 'failed')",
                ("superseded: profile changed before the package finished; "
                 "re-select the job to build a new package", now, version),
            )
        conn.commit()
        return superseded
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def delete_artifact(kind: str, ref: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE kind = ? AND ref = ?", (kind, ref))


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


# --- Posting groups (PLAN.md P8a) -------------------------------------------
# Kept current by every write that changes what grouping reads — company,
# title, location (search cards) and description (detail saves) — in
# the same transaction as that write, so there is never a window where a
# posting has moved and its group has not.

_GROUPING_COLUMNS = ("job_id", "company", "title", "location", "description",
                     "description_hash", "first_seen_at")


def _grouping_rows(conn, job_ids) -> list[dict]:
    """What grouping reads, for these postings — or, with None, for every
    posting that has a description."""
    columns = ", ".join(_GROUPING_COLUMNS)
    if job_ids is None:
        rows = conn.execute(
            f"SELECT {columns} FROM jobs WHERE description IS NOT NULL").fetchall()
    else:
        wanted = sorted(set(job_ids))
        rows = []
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            rows += conn.execute(
                f"SELECT {columns} FROM jobs "
                f"WHERE job_id IN ({', '.join('?' * len(chunk))})", chunk).fetchall()
    return [dict(zip(_GROUPING_COLUMNS, row)) for row in rows]


def _write_groups(conn, key: str, groups: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO posting_groups (job_id, match_key, group_id, rule_version) "
        "VALUES (?, ?, ?, ?)",
        [(job_id, key, group_id, duplicates.RULE_VERSION)
         for job_id, group_id in sorted(groups.items())])


def _shared(groups: dict[str, str]) -> set[frozenset[str]]:
    """The groups of two or more, as sets of postings: what a reader
    sees. A posting alone is not grouped, whatever its row says."""
    members: dict[str, set[str]] = defaultdict(set)
    for job_id, group_id in groups.items():
        members[group_id].add(job_id)
    return {frozenset(m) for m in members.values() if len(m) > 1}


def _regroup(conn, job_ids=(), *, described=()) -> bool:
    """Recompute the groups these postings were in and are in now, inside
    the caller's transaction. `job_ids` are postings whose card fields
    may have changed: only those whose bucket moved are regrouped.
    `described` are postings whose description was just saved: their
    bucket is always regrouped, since the text itself is compared.
    Returns whether any group of two or more changed."""
    ids = set(job_ids) | set(described)
    if not ids:
        return False
    stored = dict(conn.execute(
        "SELECT job_id, match_key FROM posting_groups "
        f"WHERE job_id IN ({', '.join('?' * len(ids))})", sorted(ids)).fetchall())
    current = {p["job_id"]: duplicates.match_key(p) for p in _grouping_rows(conn, ids)}
    moved = {j for j in ids if stored.get(j) != current.get(j)} | set(described)
    if not moved:
        return False
    keys = sorted(({stored.get(j) for j in moved} | {current.get(j) for j in moved})
                  - {None})
    before = {key: dict(conn.execute(
        "SELECT job_id, group_id FROM posting_groups WHERE match_key = ?",
        (key,)).fetchall()) for key in keys}
    conn.execute(f"DELETE FROM posting_groups WHERE job_id IN ({', '.join('?' * len(moved))})",
                 sorted(moved))
    changed = False
    for key in keys:
        arriving = {j for j in moved if current.get(j) == key}
        members = [p for p in _grouping_rows(conn, set(before[key]) | arriving)
                   if duplicates.match_key(p) == key]
        after = duplicates.group(members)
        conn.execute("DELETE FROM posting_groups WHERE match_key = ?", (key,))
        _write_groups(conn, key, after)
        changed |= _shared(before[key]) != _shared(after)
    return changed


def posting_groups(job_ids, run_id: int | None = None) -> dict[str, dict]:
    """For each of these postings that shares a group with another: the
    group's id and every member, oldest first, each with its link, when
    it was first seen and whether it is in `run_id`'s results."""
    wanted = sorted(set(job_ids))
    if not wanted:
        return {}
    with _connect() as conn:
        group_of = dict(conn.execute(
            "SELECT job_id, group_id FROM posting_groups "
            f"WHERE job_id IN ({', '.join('?' * len(wanted))})", wanted).fetchall())
        group_ids = sorted(set(group_of.values()))
        if not group_ids:
            return {}
        rows = conn.execute(
            "SELECT posting_groups.group_id, jobs.job_id, jobs.url, "
            "jobs.first_seen_at, EXISTS (SELECT 1 FROM search_run_jobs "
            "WHERE search_run_jobs.run_id IS ? "
            "AND search_run_jobs.job_id = jobs.job_id) "
            "FROM posting_groups JOIN jobs ON jobs.job_id = posting_groups.job_id "
            f"WHERE posting_groups.group_id IN ({', '.join('?' * len(group_ids))}) "
            "ORDER BY jobs.first_seen_at, jobs.job_id",
            (run_id, *group_ids)).fetchall()
    members: dict[str, list[dict]] = defaultdict(list)
    for group_id, job_id, url, first_seen_at, in_run in rows:
        members[group_id].append({"job_id": job_id, "url": url,
                                  "first_seen_at": first_seen_at,
                                  "in_run": bool(in_run)})
    return {job_id: {"id": group_id, "members": members[group_id]}
            for job_id, group_id in group_of.items() if len(members[group_id]) > 1}


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
        _regroup(conn, [card["job_id"]])
    return not existed


def get_job(job_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return _job_row_to_dict(row) if row else None


def job_ids_in_run(run_id: int, job_ids: list[str]) -> set[str]:
    """Which of `job_ids` actually belong to `run_id`. Membership is the
    only thing a selection can honestly be made against: the jobs table is
    a global cache spanning every run, so an id from a result set the user
    is no longer looking at resolves there perfectly well."""
    if not job_ids:
        return set()
    placeholders = ",".join("?" * len(job_ids))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id FROM search_run_jobs "
            f"WHERE run_id = ? AND job_id IN ({placeholders})",
            (run_id, *job_ids),
        ).fetchall()
    return {row[0] for row in rows}


def in_earlier_runs(run_id: int, job_ids) -> set[str]:
    """Which of `job_ids` were in a search run older than `run_id`. Run
    ids are AUTOINCREMENT, so a lower id is a run that started earlier."""
    wanted = sorted(set(job_ids))
    found: set[str] = set()
    with _connect() as conn:
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            found.update(row[0] for row in conn.execute(
                "SELECT DISTINCT job_id FROM search_run_jobs "
                f"WHERE run_id < ? AND job_id IN ({', '.join('?' * len(chunk))})",
                (run_id, *chunk)))
    return found


def list_jobs(run_id: int | None = None,
              profile_version: int | None = None) -> list[dict]:
    """All cached jobs, or — with `run_id` — only the jobs that belong to
    that search run (what the UI shows as the current results). Each job
    carries its scores via LEFT JOIN, but ONLY when the score row matches
    the job's CURRENT description hash and the given profile version —
    stale rows (mid-rescore, edited posting) surface as unscored, never
    as old numbers."""
    import json

    columns = ", ".join(f"jobs.{c}" for c in _JOB_COLUMNS)
    score_cols = (
        "job_scores.deterministic_score, job_scores.llm_rerank, "
        "job_scores.llm_rank, job_scores.breakdown_json"
    )
    join = (
        "LEFT JOIN job_scores ON job_scores.job_id = jobs.job_id "
        "AND job_scores.jd_hash IS jobs.description_hash "
        "AND job_scores.profile_version IS ?"
    )
    with _connect() as conn:
        if run_id is None:
            rows = conn.execute(
                f"SELECT {columns}, {score_cols} FROM jobs {join} "
                "ORDER BY jobs.last_seen_at DESC",
                (profile_version,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns}, {score_cols} FROM jobs "
                "JOIN search_run_jobs ON search_run_jobs.job_id = jobs.job_id "
                f"{join} WHERE search_run_jobs.run_id = ? "
                "ORDER BY jobs.last_seen_at DESC",
                (profile_version, run_id),
            ).fetchall()
    jobs = []
    for row in rows:
        job = _job_row_to_dict(row[: len(_JOB_COLUMNS)])
        job["deterministic_score"] = row[len(_JOB_COLUMNS)]
        job["llm_rerank"] = row[len(_JOB_COLUMNS) + 1]
        job["llm_rank"] = row[len(_JOB_COLUMNS) + 2]
        raw = row[len(_JOB_COLUMNS) + 3]
        job["score_breakdown"] = json.loads(raw) if raw else None
        jobs.append(job)
    return jobs


def record_search_page(run_id: int, cards: list[dict], next_start: int, cap: int) -> int:
    """Persist one results page atomically: upsert the cards into the
    global job cache, associate them with the run (up to `cap` members),
    and checkpoint the run's cursor + collected count — all in a single
    transaction, so a crash between card insertion and checkpoint can't
    desync the accounting. Idempotent: re-recording the same page after
    a crash changes nothing. Returns the run's collected total."""
    now = datetime.now(timezone.utc).isoformat()
    recorded = []
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
            recorded.append(card["job_id"])
        # A card can rename a known posting, or move it: its group follows
        # in this same transaction. The caller's "search" event re-reads
        # the job list, groups included.
        _regroup(conn, recorded)
        collected = conn.execute(
            "SELECT COUNT(*) FROM search_run_jobs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE search_runs SET next_start = ?, collected = ?, updated_at = ? "
            "WHERE id = ?",
            (next_start, collected, now, run_id),
        )
    return collected


class DetailSaved(NamedTuple):
    superseded: list[int]  # the package ids this posting edit superseded
    regrouped: bool        # whether any posting group changed


def save_job_detail(job_id: str, description: str,
                    description_hash: str) -> DetailSaved:
    """Save a posting's description. Says what that changed so the caller
    can announce it — this layer never touches the event broker."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET description = ?, description_hash = ?, "
            "detail_fetched_at = ? WHERE job_id = ?",
            (description, description_hash, now, job_id),
        )
        # A posting edit supersedes this job's unfinished packages in the
        # same transaction the hash moves — a package pinned to the old
        # text must never publish against it. Done packages keep their
        # pinned snapshot; the guarded pipeline transitions stop any
        # worker already mid-flight on a superseded row.
        superseded = [r[0] for r in conn.execute(
            "SELECT id FROM packages WHERE job_id = ? AND jd_hash != ? "
            "AND status NOT IN ('done', 'failed')", (job_id, description_hash)
        )]
        conn.execute(
            "UPDATE packages SET status = 'failed', error = ?, updated_at = ? "
            "WHERE job_id = ? AND jd_hash != ? "
            "AND status NOT IN ('done', 'failed')",
            ("superseded: posting changed before the package finished; "
             "re-select the job to build a new package", now, job_id,
             description_hash),
        )
        regrouped = _regroup(conn, described=[job_id])
    return DetailSaved(superseded, regrouped)


# --- Normalized requirements + scores (PLAN.md §4.4) ------------------------
# Requirements are content-addressed by jd_hash — two postings with the
# same description share one extraction. Scores are per job, stamped
# with the jd_hash + profile version they were computed from; a hash
# change invalidates the score cascade (invalidate_derived), while the
# requirements row keeps serving any other job with that description.

def save_requirements(jd_hash: str, requirements: dict) -> None:
    import json

    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO requirements_cache (jd_hash, requirements_json, "
            "normalized_at) VALUES (?, ?, ?) "
            "ON CONFLICT(jd_hash) DO UPDATE SET "
            "requirements_json = excluded.requirements_json, "
            "normalized_at = excluded.normalized_at",
            (jd_hash, json.dumps(requirements), now),
        )


def get_requirements(jd_hash: str | None) -> dict | None:
    import json

    if not jd_hash:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT requirements_json FROM requirements_cache WHERE jd_hash = ?",
            (jd_hash,),
        ).fetchone()
    return json.loads(row[0]) if row else None



def requirements_for(keys) -> dict[str, dict]:
    """The cached requirements under each of these cache keys that has
    them, by key. The keys are normalization's (`cache_key`), not bare
    description hashes."""
    import json

    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT jd_hash, requirements_json FROM requirements_cache "
            f"WHERE jd_hash IN ({', '.join('?' * len(wanted))})", wanted,
        ).fetchall()
    return {key: json.loads(raw) for key, raw in rows}


def save_score(
    job_id: str,
    jd_hash: str,
    profile_version: int,
    deterministic_score: float,
    breakdown: dict,
) -> None:
    """Write the deterministic baseline. Clears any llm_rerank/explanation
    from a previous scoring pass — they described the old score."""
    import json

    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO job_scores (job_id, jd_hash, profile_version, "
            "deterministic_score, breakdown_json, scored_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET jd_hash = excluded.jd_hash, "
            "profile_version = excluded.profile_version, "
            "deterministic_score = excluded.deterministic_score, "
            "breakdown_json = excluded.breakdown_json, "
            "llm_rerank = NULL, llm_rank = NULL, explanation_json = NULL, "
            "scored_at = excluded.scored_at",
            (job_id, jd_hash, profile_version, deterministic_score,
             json.dumps(breakdown), now),
        )


def set_explanation(job_id: str, explanation: dict | None,
                    profile_version: int, jd_hash: str) -> bool:
    """LLM layer only — never touches the deterministic baseline.
    Conditional: writes only if the score row still belongs to the
    (profile_version, jd_hash) the explanation was generated for — a
    row rewritten during the LLM call is left alone. Returns whether
    the write landed."""
    import json

    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE job_scores SET explanation_json = ? "
            "WHERE job_id = ? AND profile_version = ? AND jd_hash = ?",
            (json.dumps(explanation) if explanation is not None else None,
             job_id, profile_version, jd_hash),
        )
        return cursor.rowcount > 0


def apply_rerank(writes: list[tuple[str, str, float, int]],
                 profile_version: int) -> bool:
    """Atomically apply a rerank pass: `writes` is
    [(job_id, jd_hash, rerank, rank)]. Every target row must still match
    (profile_version, jd_hash) — one moved row (profile confirmed or JD
    refreshed during the LLM call) aborts the whole cluster with zero
    writes. Returns whether the cluster was applied."""
    with _connect() as conn:
        for job_id, jd_hash, _, _ in writes:
            row = conn.execute(
                "SELECT 1 FROM job_scores WHERE job_id = ? "
                "AND profile_version = ? AND jd_hash = ?",
                (job_id, profile_version, jd_hash),
            ).fetchone()
            if row is None:
                return False
        for job_id, jd_hash, rerank, rank in writes:
            conn.execute(
                "UPDATE job_scores SET llm_rerank = ?, llm_rank = ? "
                "WHERE job_id = ? AND profile_version = ? AND jd_hash = ?",
                (rerank, rank, job_id, profile_version, jd_hash),
            )
    return True


def scoring_pending(run_id: int, profile_version: int) -> int:
    """How many of the run's jobs still lack a score computed from the
    CURRENT description hash and profile version — the pipeline's
    remaining-work count (0 = fully scored)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM search_run_jobs "
            "JOIN jobs ON jobs.job_id = search_run_jobs.job_id "
            "LEFT JOIN job_scores ON job_scores.job_id = jobs.job_id "
            "WHERE search_run_jobs.run_id = ? AND ("
            "  job_scores.job_id IS NULL "
            "  OR job_scores.profile_version != ? "
            "  OR job_scores.jd_hash IS NOT jobs.description_hash)",
            (run_id, profile_version),
        ).fetchone()[0]


def get_score(job_id: str) -> dict | None:
    import json

    with _connect() as conn:
        row = conn.execute(
            "SELECT jd_hash, profile_version, deterministic_score, "
            "breakdown_json, llm_rerank, llm_rank, explanation_json "
            "FROM job_scores WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "jd_hash": row[0],
        "profile_version": row[1],
        "deterministic_score": row[2],
        "breakdown": json.loads(row[3]),
        "llm_rerank": row[4],
        "llm_rank": row[5],
        "explanation": json.loads(row[6]) if row[6] else None,
    }


def invalidate_derived(job_id: str) -> None:
    """The job's JD hash moved: its score and explanation are stale —
    drop them so the pipeline recomputes (PLAN.md §4.4). The
    content-addressed requirements cache is untouched: the old row may
    still serve other jobs with that description, and the new hash
    simply misses."""
    with _connect() as conn:
        conn.execute("DELETE FROM job_scores WHERE job_id = ?", (job_id,))


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


def supersede_running_runs(except_id: int | None = None) -> list[int]:
    """Mark every 'running' run other than `except_id` as superseded —
    a terminal state recovery never reclaims. Enforces the one-active-
    search invariant that latest-only recovery depends on."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM search_runs WHERE status = 'running' AND id != ?",
            (except_id if except_id is not None else -1,),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "UPDATE search_runs SET status = 'superseded', "
                "message = 'Superseded by a newer search.', updated_at = ? "
                f"WHERE id IN ({', '.join('?' for _ in ids)})",
                (now, *ids),
            )
    return ids




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


# --- Packages (tailoring state machine, PLAN.md §4.5/§4.6) ------------------
# A package is keyed by exactly the inputs its documents were built from:
# (job_id, profile_version, jd_hash, page_limit, template_version,
# cover_template_version). Re-selecting the same job with the same inputs
# returns the existing row — a completed package is never regenerated. A
# new profile version, an edited posting (hash change), a different page
# limit, or an upgraded document template is a different package; the
# limit is pinned at creation so a later settings change can never make
# the generation prompt and the fitter disagree, and the template
# versions are pinned for the same reason plus one more: a published
# package is immutable, so the only way an improved template can reach a
# job the user already generated is by making the next selection a new
# package. `stage_json` persists
# per-stage intermediate data (tailored JSON, validation verdicts,
# fitted documents) so a restart resumes at the stage boundary instead
# of re-spending LLM calls; `provenance_json` records what actually
# built the artifacts (PLAN.md §4.6).
#
# The inputs identify a package; `attempt` numbers the builds of it
# (PLAN.md §4.5a). Selection makes attempt 1 and afterwards returns the
# latest attempt. Only the user's Try again or Regenerate makes another
# — a fresh row with its own stage state, validation cache and repair
# ledger — and never by itself; earlier attempts and their files stay
# exactly as they were.

PACKAGE_IDENTITY = (
    "job_id", "profile_version", "jd_hash", "page_limit",
    "template_version", "cover_template_version",
)
_IDENTITY_MATCH = " AND ".join(f"{column} = ?" for column in PACKAGE_IDENTITY)
_PACKAGE_COLUMNS = (
    "id", *PACKAGE_IDENTITY, "attempt", "retry_of", "status",
    "stage_json", "error", "retry_count", "provenance_json", "output_dir",
    "created_at", "updated_at",
)
# What a user calls each input when a retry found it changed.
_INPUT_NAMES = (
    ("profile_version", "profile"), ("jd_hash", "posting"),
    ("page_limit", "page limit"), ("template_version", "templates"),
    ("cover_template_version", "templates"),
)


def _package_row_to_dict(row) -> dict:
    import json

    package = dict(zip(_PACKAGE_COLUMNS, row))
    package["stage"] = json.loads(package.pop("stage_json") or "{}")
    package["provenance"] = json.loads(package.pop("provenance_json") or "{}")
    return package


def create_package(job_id: str, profile_version: int, jd_hash: str,
                   page_limit: int = 1, *, template_version: int,
                   cover_template_version: int) -> tuple[int, bool]:
    """(package id, created). Idempotent per identity: a package that
    exists for these inputs — whatever its status — is returned as its
    latest attempt, not duplicated.

    The template versions have no default on purpose. They are the
    running code's, and a caller that forgets them would pin a package to
    documents it was not built from.
    """
    now = datetime.now(timezone.utc).isoformat()
    identity = (job_id, profile_version, jd_hash, page_limit,
                template_version, cover_template_version)
    with _connect() as conn:
        cursor = conn.execute(
            f"INSERT INTO packages ({', '.join(PACKAGE_IDENTITY)}, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT({', '.join(PACKAGE_IDENTITY)}, attempt) DO NOTHING",
            (*identity, now, now),
        )
        if cursor.rowcount > 0:
            return cursor.lastrowid, True
        row = conn.execute(
            f"SELECT id FROM packages WHERE {_IDENTITY_MATCH} "
            "ORDER BY attempt DESC LIMIT 1", identity,
        ).fetchone()
        return row[0], False


class RetryRefused(Exception):
    """A retry the package's current state does not allow. `reason` is
    one of not_found, not_finished, not_latest, key_reused."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


_FINISHED = ("done", "failed")


def _answered_retry(conn, source_id: int, request_key: str) -> int | None:
    made = conn.execute(
        "SELECT source_id, package_id FROM package_requests WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    if made is None:
        return None
    if made[0] != source_id:
        raise RetryRefused("key_reused", "that request key was used for another package")
    return made[1]


def answered_retry(source_id: int, request_key: str) -> int | None:
    """The package an earlier submission of this click got, or None if
    the click is new. Raises RetryRefused(key_reused) for a key that
    belongs to a click on another package."""
    with _connect() as conn:
        return _answered_retry(conn, source_id, request_key)


def retry_package(source_id: int, request_key: str, *, profile_version: int,
                  jd_hash: str, page_limit: int, template_version: int,
                  cover_template_version: int) -> tuple[int, bool]:
    """Try again on a failed package or Regenerate a done one. Returns
    (package id, created). The source row is never written.

    Only the job's newest package can be retried — the one row the list
    offers the button on. The inputs given are the current ones, and
    the answer is the package for them: the next attempt of the
    source's identity when nothing changed, otherwise a package for the
    current inputs, never another attempt of the old identity. When the
    current inputs' latest attempt is still being built, that one is the
    answer and nothing new is made. When it is finished, done or failed,
    the click is asking for a build and gets its next attempt, so a
    retry always does something.

    `request_key` is the client's key for one click, stored unique with
    the package that click got, whether it made that row or found it: a
    repeated or concurrent submission of the click gets the same package.
    The whole check-and-insert is one BEGIN IMMEDIATE transaction, so two
    different clicks on one package cannot both find it the newest: the
    second is refused rather than making another.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        made = _answered_retry(conn, source_id, request_key)
        if made is not None:
            conn.rollback()
            return made, False
        source = conn.execute(
            "SELECT job_id, status FROM packages WHERE id = ?", (source_id,),
        ).fetchone()
        if source is None:
            raise RetryRefused("not_found", f"no package {source_id}")
        job_id, status = source
        if status not in _FINISHED:
            raise RetryRefused("not_finished", "this package is still being built")
        newest = conn.execute(
            "SELECT max(id) FROM packages WHERE job_id = ?", (job_id,),
        ).fetchone()[0]
        if newest != source_id:
            raise RetryRefused("not_latest",
                               "a newer package for this job already exists")
        identity = (job_id, profile_version, jd_hash, page_limit,
                    template_version, cover_template_version)
        latest = conn.execute(
            f"SELECT id, attempt, status FROM packages WHERE {_IDENTITY_MATCH} "
            "ORDER BY attempt DESC LIMIT 1", identity,
        ).fetchone()
        if latest is not None and latest[2] not in _FINISHED:
            package_id, created = latest[0], False
        else:
            cursor = conn.execute(
                f"INSERT INTO packages ({', '.join(PACKAGE_IDENTITY)}, attempt, "
                "retry_of, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*identity, latest[1] + 1 if latest else 1, source_id, now, now),
            )
            package_id, created = cursor.lastrowid, True
        conn.execute(
            "INSERT INTO package_requests (request_key, source_id, package_id, "
            "created_at) VALUES (?, ?, ?, ?)",
            (request_key, source_id, package_id, now),
        )
        conn.commit()
        return package_id, created
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def supersede_outdated_template_packages(template_version: int,
                                         cover_template_version: int) -> int:
    """Fail unfinished packages pinned to superseded templates; returns
    the count.

    An upgrade can land between a package being queued and being built.
    Finishing it would render the new templates into a row whose identity
    says otherwise, and the provenance would disagree with the key. Same
    treatment as a profile change: unfinished work is superseded, the
    user re-selects, and done packages are history — untouched, still
    downloadable, still exactly the documents they shipped.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE packages SET status = 'failed', error = ?, updated_at = ? "
            "WHERE (template_version != ? OR cover_template_version != ?) "
            "AND status NOT IN ('done', 'failed')",
            ("superseded: the document templates were upgraded before the "
             "package finished; re-select the job to build a new package",
             now, template_version, cover_template_version),
        )
        return cursor.rowcount


def get_package(package_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_PACKAGE_COLUMNS)} FROM packages WHERE id = ?",
            (package_id,),
        ).fetchone()
    return _package_row_to_dict(row) if row else None


def update_package(
    package_id: int,
    *,
    status: str | None = None,
    stage: object = _UNSET,
    error: object = _UNSET,
    retry_count: int | None = None,
    provenance: object = _UNSET,
    output_dir: str | None = None,
    guard_not_terminal: bool = False,
) -> bool:
    """Persist a state-machine transition; returns whether a row was
    written. `stage`/`provenance` take dicts; `error`/`stage`/
    `provenance` use the sentinel so passing None explicitly clears the
    column (omit to leave it). With `guard_not_terminal` the write
    no-ops against a done/failed row — a worker still driving a package
    that a JD/profile change just superseded can never resurrect it."""
    import json

    sets, params = ["updated_at = ?"], [datetime.now(timezone.utc).isoformat()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if retry_count is not None:
        sets.append("retry_count = ?")
        params.append(retry_count)
    if output_dir is not None:
        sets.append("output_dir = ?")
        params.append(output_dir)
    for column, value in (("stage_json", stage), ("provenance_json", provenance)):
        if value is not _UNSET:
            sets.append(f"{column} = ?")
            params.append(json.dumps(value) if value is not None else None)
    if error is not _UNSET:
        sets.append("error = ?")
        params.append(error)
    params.append(package_id)
    where = "id = ?"
    if guard_not_terminal:
        where += " AND status NOT IN ('done', 'failed')"
    with _connect() as conn:
        cursor = conn.execute(
            f"UPDATE packages SET {', '.join(sets)} WHERE {where}", params)
    return cursor.rowcount > 0


def list_packages() -> list[dict]:
    """Every package, newest first, with its job's display fields.

    A row a retry made carries `inputs_changed`: what the user would call
    each input that differs from the package they retried ("profile",
    "posting", "page limit", "templates"). Empty for another attempt of
    the same inputs, and for a row that was selected rather than retried.
    """
    n = len(_PACKAGE_COLUMNS)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join('packages.' + c for c in _PACKAGE_COLUMNS)}, "
            "jobs.title, jobs.company, jobs.url, "
            f"{', '.join('source.' + c for c, _ in _INPUT_NAMES)} "
            "FROM packages JOIN jobs ON jobs.job_id = packages.job_id "
            "LEFT JOIN packages AS source ON source.id = packages.retry_of "
            "ORDER BY packages.id DESC"
        ).fetchall()
    packages = []
    for row in rows:
        package = _package_row_to_dict(row[:n])
        package["title"], package["company"], package["url"] = row[n:n + 3]
        source = row[n + 3:]
        changed = []
        if package["retry_of"] is not None and source[0] is not None:
            for (column, name), was in zip(_INPUT_NAMES, source):
                if package[column] != was and name not in changed:
                    changed.append(name)
        package["inputs_changed"] = changed
        packages.append(package)
    return packages


def packages_to_resume() -> list[dict]:
    """Packages a previous process left mid-flight — everything not in a
    terminal state, oldest first so batch order is preserved on restart."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_PACKAGE_COLUMNS)} FROM packages "
            "WHERE status NOT IN ('done', 'failed') ORDER BY id"
        ).fetchall()
    return [_package_row_to_dict(row) for row in rows]


# --- Application tracker (PLAN.md P8b) ---------------------------------------
# The user's own record of what they did with a posting. Never a gate:
# nothing waits on it, and nothing in the pipeline reads it.

APPLICATION_STATUSES = ("applied", "interviewing", "offer", "rejected", "withdrawn")
# Appended by "Clear status": the posting is untracked again from this
# event on. Not an undo — what came before stays in the history.
CLEARED = "cleared"


class ApplicationRefused(Exception):
    """An event the posting can't take. `reason` is unknown_job or
    foreign_package."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def record_application(job_id: str, status: str, occurred_on: str,
                       package_id: int | None = None) -> bool:
    """Append one status the user set on a posting; True when appended.

    A package, when given, must be one of the posting's own. An event
    that would change nothing is not appended — the posting's current
    status, day and package again, or a clear of a posting that isn't
    tracked — so a click sent twice is one event. Check and append are
    one BEGIN IMMEDIATE transaction.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
            raise ApplicationRefused("unknown_job", f"Unknown job {job_id}")
        if package_id is not None:
            owner = conn.execute("SELECT job_id FROM packages WHERE id = ?",
                                 (package_id,)).fetchone()
            if owner is None or owner[0] != job_id:
                raise ApplicationRefused(
                    "foreign_package", f"Package {package_id} is not one of this posting's.")
        current = conn.execute(
            "SELECT status, occurred_on, package_id FROM application_events "
            "WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,),
        ).fetchone()
        if status == CLEARED:
            unchanged = current is None or current[0] == CLEARED
        else:
            unchanged = current == (status, occurred_on, package_id)
        if unchanged:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO application_events (job_id, package_id, status, "
            "occurred_on, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, package_id, status, occurred_on, now),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


_APPLICATION_COLUMNS = ("event_id", "job_id", "package_id", "status", "occurred_on")


def current_applications() -> list[dict]:
    """Every tracked posting, by its event with the highest id. A posting
    with no events, or whose latest is a clear, is untracked and absent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, job_id, package_id, status, occurred_on FROM application_events "
            "WHERE id IN (SELECT max(id) FROM application_events GROUP BY job_id) "
            "AND status != 'cleared' ORDER BY id"
        ).fetchall()
    return [dict(zip(_APPLICATION_COLUMNS, row)) for row in rows]
