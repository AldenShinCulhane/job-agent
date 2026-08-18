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
CREATE TABLE IF NOT EXISTS packages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    jd_hash         TEXT NOT NULL,
    page_limit      INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'queued',
    stage_json      TEXT,
    error           TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    provenance_json TEXT,
    output_dir      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (job_id, profile_version, jd_hash, page_limit)
);
"""

# One-shot schema fixes for databases created by earlier revisions of
# this branch (executescript's IF NOT EXISTS cannot alter shapes).
_MIGRATIONS = (
    # requirements were briefly keyed per-job; the cache is now
    # content-addressed by jd_hash (cheap to regenerate — just drop).
    "DROP TABLE IF EXISTS job_requirements",
    # rerank rank ordinal added after the table first shipped.
    "ALTER TABLE job_scores ADD COLUMN llm_rank INTEGER",
)


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
        conn.executescript(_SCHEMA)
        for statement in _MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # already applied (e.g. duplicate column)
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
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE profile_versions SET confirmed = 1 WHERE version = ?", (version,)
        )
        # Unfinished packages pinned to any other profile version are now
        # stale inputs: without this they strand forever (job_scores only
        # keeps the current profile's row, so their waiting_inputs check
        # never passes) or publish against a profile the user replaced.
        # Done packages are history and stay untouched.
        conn.execute(
            "UPDATE packages SET status = 'failed', error = ?, updated_at = ? "
            "WHERE profile_version != ? AND status NOT IN ('done', 'failed')",
            ("superseded: profile changed before the package finished; "
             "re-select the job to build a new package", now, version),
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
        # A posting edit supersedes this job's unfinished packages in the
        # same transaction the hash moves — a package pinned to the old
        # text must never publish against it. Done packages keep their
        # pinned snapshot; the guarded pipeline transitions stop any
        # worker already mid-flight on a superseded row.
        conn.execute(
            "UPDATE packages SET status = 'failed', error = ?, updated_at = ? "
            "WHERE job_id = ? AND jd_hash != ? "
            "AND status NOT IN ('done', 'failed')",
            ("superseded: posting changed before the package finished; "
             "re-select the job to build a new package", now, job_id,
             description_hash),
        )


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
# (job_id, profile_version, jd_hash, page_limit). Re-selecting the same
# job with the same inputs returns the existing row — a completed
# package is never regenerated. A new profile version, an edited posting
# (hash change), or a different page limit is a different package; the
# limit is pinned at creation so a later settings change can never make
# the generation prompt and the fitter disagree. `stage_json` persists
# per-stage intermediate data (tailored JSON, validation verdicts,
# fitted documents) so a restart resumes at the stage boundary instead
# of re-spending LLM calls; `provenance_json` records what actually
# built the artifacts (PLAN.md §4.6).

_PACKAGE_COLUMNS = (
    "id", "job_id", "profile_version", "jd_hash", "page_limit", "status",
    "stage_json", "error", "retry_count", "provenance_json", "output_dir",
    "created_at", "updated_at",
)


def _package_row_to_dict(row) -> dict:
    import json

    package = dict(zip(_PACKAGE_COLUMNS, row))
    package["stage"] = json.loads(package.pop("stage_json") or "{}")
    package["provenance"] = json.loads(package.pop("provenance_json") or "{}")
    return package


def create_package(job_id: str, profile_version: int, jd_hash: str,
                   page_limit: int = 1) -> tuple[int, bool]:
    """(package id, created). Idempotent per (job, profile, hash, page
    limit): an existing package — whatever its status — is returned, not
    duplicated."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO packages (job_id, profile_version, jd_hash, "
            "page_limit, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id, profile_version, jd_hash, page_limit) "
            "DO NOTHING",
            (job_id, profile_version, jd_hash, page_limit, now, now),
        )
        if cursor.rowcount > 0:
            return cursor.lastrowid, True
        row = conn.execute(
            "SELECT id FROM packages WHERE job_id = ? AND profile_version = ? "
            "AND jd_hash = ? AND page_limit = ?",
            (job_id, profile_version, jd_hash, page_limit),
        ).fetchone()
        return row[0], False


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
    """Every package, newest first, with its job's display fields."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join('packages.' + c for c in _PACKAGE_COLUMNS)}, "
            "jobs.title, jobs.company, jobs.url FROM packages "
            "JOIN jobs ON jobs.job_id = packages.job_id "
            "ORDER BY packages.id DESC"
        ).fetchall()
    packages = []
    for row in rows:
        package = _package_row_to_dict(row[: len(_PACKAGE_COLUMNS)])
        package["title"], package["company"], package["url"] = row[len(_PACKAGE_COLUMNS):]
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
