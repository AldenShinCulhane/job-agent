"""Package state machine (PLAN.md §4.5/§4.6): generate → validate →
fit → assert → publish, fully autonomous per package.

Every transition persists to SQLite, and stage_json carries the
intermediate data each stage needs, so a restart resumes at the stage
boundary — a package whose generation already succeeded never spends
those LLM calls again, and a done package is never regenerated.

Engine-failure routing mirrors the scoring pipeline: UsageExhausted is
a durable waiting state (waiting_usage) the retry timer resumes;
auth/config/entitlement problems park the package back in 'queued' with
the error recorded and stop the batch (engine recovery resumes it);
anything else fails only this package — failures are isolated.
"""

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.core import db, llm
from backend.normalize.requirements import cache_key
from backend.resume import facts
from backend.tailor import compile as latex
from backend.tailor import page_limit
from backend.tailor import resume as tailor_resume
from backend.tailor import validate

log = logging.getLogger(__name__)

OUTPUT_ROOT = Path(__file__).resolve().parent.parent.parent / "output"

# Resume page limit (PLAN.md §4.5: user-set, default 1). Stored in
# engine_state; the settings surface arrives with M5.
PAGE_LIMIT_KEY = "resume_page_limit"


def resume_page_limit() -> int:
    try:
        return max(1, int(db.get_state(PAGE_LIMIT_KEY, "1")))
    except (TypeError, ValueError):
        return 1


def _slug(text: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", text or "")
    return "_".join(parts)[:40] or "package"


def output_dir_for(job: dict) -> Path:
    return OUTPUT_ROOT / f"{_slug(job['company'])}_{job['job_id']}"


def _job_md(job: dict, snapshot: str, score: dict | None) -> str:
    lines = [
        f"# {job['title']} — {job['company']}",
        "",
        f"- Posting: {job['url']}",
        f"- Location: {job.get('location') or 'n/a'}",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
    ]
    if score is not None:
        lines.append(f"- Deterministic score: {score['deterministic_score']}")
        breakdown = score.get("breakdown") or {}
        for label, key in (("Match reasons", "match_reasons"),
                           ("Gaps", "gap_reasons")):
            reasons = breakdown.get(key) or []
            if reasons:
                lines.append(f"- {label}: " + "; ".join(reasons))
    lines += ["", "## Job description (pinned snapshot)", "", snapshot]
    return "\n".join(lines)


def _transition(package_id: int, status: str, **kwargs) -> None:
    db.update_package(package_id, status=status, **kwargs)
    log.info("package %s -> %s", package_id, status)


def _score_if_current(pkg: dict) -> dict | None:
    score = db.get_score(pkg["job_id"])
    if score is None:
        return None
    if (score["jd_hash"] != pkg["jd_hash"]
            or score["profile_version"] != pkg["profile_version"]):
        return None
    return score


async def run_package(package_id: int) -> str:
    """Drive one package to a terminal or waiting state; returns the
    final status. Never raises except CancelledError (shutdown — the
    row keeps its mid-flight status for restart recovery)."""
    pkg = db.get_package(package_id)
    if pkg is None:
        return "failed"
    if pkg["status"] in ("done", "failed"):
        return pkg["status"]  # completed work is never redone
    if pkg["status"] != "queued":  # resuming a mid-flight package
        db.update_package(package_id, retry_count=pkg["retry_count"] + 1)

    job = db.get_job(pkg["job_id"])
    profile = db.get_profile(pkg["profile_version"])
    if job is None or profile is None:
        _transition(package_id, "failed", error="job or profile row is gone")
        return "failed"
    ledger = facts.build_ledger(profile)
    stage = pkg["stage"]

    try:
        if "tailored" not in stage:
            # The package is pinned to the hash it was created for; a
            # posting edited before generation started is a different
            # input set — fail cleanly rather than tailor to stale text.
            if job.get("description_hash") != pkg["jd_hash"]:
                _transition(package_id, "failed",
                            error="posting changed before generation; "
                                  "re-select the job to build a new package")
                return "failed"
            _transition(package_id, "generating")
            requirements = db.get_requirements(cache_key(pkg["jd_hash"]))
            score = _score_if_current(pkg)
            tailored = await tailor_resume.generate_tailored(
                profile, ledger, job, requirements,
                (score or {}).get("breakdown"), resume_page_limit(),
            )
            stage = {"tailored": tailored, "jd_snapshot": job["description"],
                     "entail_cache": {}}
            _transition(package_id, "validating", stage=stage)

        if not stage.get("validated"):
            _transition(package_id, "validating")
            cache = stage.get("entail_cache", {})
            failures = await validate.validate(stage["tailored"], profile,
                                               ledger, cache)
            if failures:
                repaired = await validate.repair(stage["tailored"], failures,
                                                 profile, ledger)
                stage["tailored"] = repaired
                db.update_package(package_id, stage=stage)
                failures = await validate.validate(repaired, profile, ledger,
                                                   cache)
            stage["entail_cache"] = cache
            if failures:
                detail = "; ".join(
                    f"{f['claim_id']}: {f['reason']}" for f in failures[:5])
                _transition(package_id, "failed", stage=stage,
                            error=f"truth validation failed after repair: {detail}")
                return "failed"
            stage["validated"] = True
            _transition(package_id, "fitting", stage=stage)

        # Fitting is deterministic given the validated JSON (the one LLM
        # path — cover condense — revalidates internally), so a restart
        # simply re-runs it; nothing partial is persisted.
        _transition(package_id, "fitting")
        workdir = db.DATA_DIR / "build" / f"pkg{package_id}"
        cache = stage.get("entail_cache", {})
        limit = resume_page_limit()
        resume_fit = await page_limit.fit_resume(
            stage["tailored"], profile, ledger, cache, limit,
            workdir / "resume")
        cover_fit = await page_limit.fit_cover(
            stage["tailored"], profile, ledger, cache, job,
            workdir / "cover")
        stage["entail_cache"] = cache

        _transition(package_id, "asserting", stage=stage)
        # Hard assertions (PLAN.md §4.5 step 4) — the fit loops enforce
        # these already; assert anyway so nothing unvalidated or
        # oversized can ever ship even if a fit loop regresses.
        if resume_fit.result.pages > limit or cover_fit.result.pages != 1:
            raise page_limit.FitError(
                f"assertion failed: resume {resume_fit.result.pages}/{limit} "
                f"pages, cover {cover_fit.result.pages}/1 pages")
        if not stage.get("validated"):
            raise page_limit.ValidationFailed([])

        _transition(package_id, "publishing")
        final_dir = output_dir_for(job)
        tmp_dir = final_dir.with_name(final_dir.name + ".publishing")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        shutil.copy2(resume_fit.result.pdf_path, tmp_dir / "resume.pdf")
        shutil.copy2(cover_fit.result.pdf_path, tmp_dir / "cover_letter.pdf")
        (tmp_dir / "job.md").write_text(
            _job_md(job, stage.get("jd_snapshot") or "", _score_if_current(pkg)))
        if final_dir.exists():
            shutil.rmtree(final_dir)
        tmp_dir.rename(final_dir)  # single-step publish
        shutil.rmtree(workdir, ignore_errors=True)

        provenance = {
            "model": db.get_state("pinned_model"),
            "engine_resume": resume_fit.result.engine,
            "engine_cover": cover_fit.result.engine,
            "prompt_version": tailor_resume.PROMPT_VERSION,
            "schema_version": tailor_resume.SCHEMA_VERSION,
            "profile_version": pkg["profile_version"],
            "jd_hash": pkg["jd_hash"],
            "page_limit": limit,
            "resume_pages": resume_fit.result.pages,
            "cover_pages": cover_fit.result.pages,
            "trimmed_bullets": len(resume_fit.trimmed),
            "retry_count": db.get_package(package_id)["retry_count"],
        }
        _transition(package_id, "done", stage=stage, error=None,
                    provenance=provenance, output_dir=str(final_dir))
        return "done"

    except asyncio.CancelledError:
        raise  # shutdown: status stays mid-flight for startup recovery
    except llm.UsageExhausted as exc:
        _transition(package_id, "waiting_usage", stage=stage, error=str(exc))
        return "waiting_usage"
    except (llm.AuthRequired, llm.ModelNotEntitled, llm.ConfigError) as exc:
        # Human-actionable engine problem: nothing this package did wrong.
        # Park it queued with the reason; engine recovery resumes the batch.
        _transition(package_id, "queued", stage=stage,
                    error=f"{type(exc).__name__}: {exc}")
        return "engine_blocked"
    except (llm.EngineError, latex.CompileError, page_limit.FitError,
            page_limit.ValidationFailed) as exc:
        _transition(package_id, "failed", stage=stage,
                    error=f"{type(exc).__name__}: {exc}")
        return "failed"
    except Exception as exc:  # noqa: BLE001 — isolation: only this package fails
        log.exception("package %s failed unexpectedly", package_id)
        _transition(package_id, "failed", stage=stage,
                    error=f"{type(exc).__name__}: {exc}")
        return "failed"


async def run_batch() -> str:
    """Process every resumable package in batch order. Returns why the
    batch stopped: 'idle' (nothing left to do), 'waiting_usage' (durable
    engine wait — caller arms the retry timer), or 'engine_blocked'
    (caller resumes after engine recovery)."""
    while True:
        rows = [p for p in db.packages_to_resume()
                if p["status"] != "waiting_usage"]
        waiting = [p for p in db.packages_to_resume()
                   if p["status"] == "waiting_usage"]
        if not rows and waiting:
            # Their cooldown may have passed (e.g. after a restart).
            rows = waiting
        if not rows:
            return "idle"
        for pkg in rows:
            status = await run_package(pkg["id"])
            if status == "waiting_usage":
                return "waiting_usage"
            if status == "engine_blocked":
                return "engine_blocked"
        # Loop again: a package parked back to queued mid-list, or new
        # selections queued while we ran, get picked up before exiting.
