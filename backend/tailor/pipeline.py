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
import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.core import db, llm
from backend.normalize.requirements import cache_key
from backend.resume import facts, template
from backend.tailor import compile as latex
from backend.tailor import cover as cover_mod
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


def output_dir_for(job: dict, package_id: int) -> Path:
    """Immutable per-package directory: the package id keys the exact
    input set (job, profile version, JD hash, page limit), so a new
    package for the same job — new profile, edited posting — publishes
    beside the old one, never over it."""
    return OUTPUT_ROOT / f"{_slug(job['company'])}_{job['job_id']}_p{package_id}"


def _content_sha(obj) -> str:
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True).encode()).hexdigest()


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


class Superseded(Exception):
    """The package was invalidated (JD or profile changed) while a worker
    was driving it — the guarded write no-oped, so stop touching it."""


def _transition(package_id: int, status: str | None = None, **kwargs) -> None:
    """Guarded state write: every pipeline write refuses to touch a
    terminal row, so a JD/profile supersede that lands mid-flight wins —
    the worker's next persist raises instead of resurrecting the row."""
    if not db.update_package(package_id, status=status,
                             guard_not_terminal=True, **kwargs):
        raise Superseded(f"package {package_id} reached a terminal state "
                         "outside this worker")
    if status is not None:
        log.info("package %s -> %s", package_id, status)


def _park(package_id: int, status: str, stage: dict, error: str) -> bool:
    """Exception-handler write; False when the package was superseded in
    the meantime (nothing written, the terminal state stands)."""
    try:
        _transition(package_id, status, stage=stage, error=error)
        return True
    except Superseded:
        return False


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
        db.update_package(package_id, retry_count=pkg["retry_count"] + 1,
                          guard_not_terminal=True)

    job = db.get_job(pkg["job_id"])
    profile = db.get_profile(pkg["profile_version"])
    if job is None or profile is None:
        _transition(package_id, "failed", error="job or profile row is gone")
        return "failed"
    ledger = facts.build_ledger(profile)
    stage = pkg["stage"]

    # The page limit is pinned at creation — part of package identity —
    # so the generation prompt and the fitter can never target different
    # limits, and a settings change never mutates an existing package.
    limit = pkg["page_limit"]
    workdir = db.DATA_DIR / "build" / f"pkg{package_id}"

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
            # Generation quality depends on the derived inputs — a
            # package selected before scoring finished waits (no gate:
            # scoring completion resumes it) rather than permanently
            # baking in requirements=None.
            requirements = db.get_requirements(cache_key(pkg["jd_hash"]))
            score = _score_if_current(pkg)
            if requirements is None or score is None:
                _transition(
                    package_id,
                    error="waiting for scoring to derive this job's "
                          "requirements and score; resumes automatically")
                return "waiting_inputs"
            _transition(package_id, "generating", error=None)
            tailored = await tailor_resume.generate_tailored(
                profile, ledger, job, requirements, score.get("breakdown"),
                limit,
            )
            # Snapshot what this call ACTUALLY used the moment it
            # succeeds — provenance and job.md publish from these, so an
            # engine change, code upgrade, or re-score between now and
            # publish can never misdescribe the artifacts. The card
            # fields join the pin too: a search refresh can rewrite
            # company/title/URL without moving the JD hash, and the
            # cover greeting, output path, and job.md must describe the
            # posting this package was generated for.
            stage = {
                "tailored": tailored,
                "jd_snapshot": job["description"],
                "job_snapshot": {
                    key: job.get(key)
                    for key in ("job_id", "title", "company", "location",
                                "url", "posted_text", "workplace",
                                "easy_apply")
                },
                "entail_cache": {},
                # Append-only ledger of every LLM call this package ever
                # made, each entry written the moment its call returns —
                # a later failure can park the package but never lose or
                # misattribute spend that already happened.
                "llm_calls": [{
                    "call": "generation",
                    "model": db.get_state("pinned_model"),
                    "effort": llm.effective_effort("tailor"),
                    "prompt_version": tailor_resume.PROMPT_VERSION,
                    "schema_version": tailor_resume.SCHEMA_VERSION,
                }],
                "generation": {
                    "prompt_version": tailor_resume.PROMPT_VERSION,
                    "schema_version": tailor_resume.SCHEMA_VERSION,
                    "requirements": requirements,
                    "score": {
                        "deterministic_score": score["deterministic_score"],
                        "breakdown": score.get("breakdown") or {},
                    },
                },
            }
            _transition(package_id, "validating", stage=stage)

        # Every later LLM call checkpoints itself into the append-only
        # ledger the moment it returns; the recorder persists in place so
        # a failure right after a call can never lose its record.
        def _record(call: str, task: str) -> None:
            stage.setdefault("llm_calls", []).append({
                "call": call,
                "model": db.get_state("pinned_model"),
                "effort": llm.effective_effort(task),
            })
            _transition(package_id, stage=stage)

        def record_entailment(call: str) -> None:
            _record(call, "entail")

        # Validation is bound to the exact content it approved: the hash
        # guard means repaired-or-edited JSON can never coast on a stale
        # flag, and the asserting stage re-checks it before publish.
        if stage.get("validated_sha") != _content_sha(stage["tailored"]):
            _transition(package_id, "validating")
            # setdefault: the recorder persists `stage`, so the cache the
            # entailment pass mutates must already live inside it — the
            # ledger entry and its verdicts land in the same write.
            cache = stage.setdefault("entail_cache", {})
            failures = await validate.validate(stage["tailored"], profile,
                                               ledger, cache,
                                               record=record_entailment)
            if failures:
                repaired = await validate.repair(stage["tailored"], failures,
                                                 profile, ledger)
                # The repaired JSON and its call metadata persist in ONE
                # write, BEFORE revalidation — an interruption here can
                # never make the record claim the repair didn't happen.
                stage["tailored"] = repaired
                stage.setdefault("llm_calls", []).append({
                    "call": "truth_repair",
                    "model": db.get_state("pinned_model"),
                    "effort": llm.effective_effort("repair"),
                })
                _transition(package_id, stage=stage)
                failures = await validate.validate(repaired, profile, ledger,
                                                   cache,
                                                   record=record_entailment)
            stage["entail_cache"] = cache
            if failures:
                detail = "; ".join(
                    f"{f['claim_id']}: {f['reason']}" for f in failures[:5])
                _transition(package_id, "failed", stage=stage,
                            error=f"truth validation failed after repair: {detail}")
                return "failed"
            stage["validated_sha"] = _content_sha(stage["tailored"])
            _transition(package_id, "fitting", stage=stage)

        # Each fit persists its result (fitted JSON + layout) before the
        # next stage starts: the LLM-bearing fit paths (cover condense,
        # trim revalidation) never re-run once their document is
        # recorded — a restart recompiles the persisted state instead.
        # Everything after generation renders and publishes against the
        # PINNED job card, not the live row — a search refresh that
        # rewrites company/title/URL without moving the JD hash must not
        # leak into the greeting, output path, or job.md.
        job_snap = stage.get("job_snapshot") or job

        cache = stage.setdefault("entail_cache", {})
        if "resume_fit" not in stage:
            _transition(package_id, "fitting")
            resume_fit = await page_limit.fit_resume(
                stage["tailored"], profile, ledger, cache, limit,
                workdir / "resume", record=record_entailment)
            stage["resume_fit"] = page_limit.fit_state(resume_fit)
            stage["entail_cache"] = cache
            _transition(package_id, stage=stage)
        if "cover_fit" not in stage:
            _transition(package_id, "fitting")

            def _on_condensed(cover_letter: dict) -> None:
                # The xhigh condense call just returned: persist its
                # output AND its ledger entry before revalidation can
                # fail — a resume reuses this instead of paying again.
                stage["cover_condensed"] = cover_letter
                stage["entail_cache"] = cache
                _record("cover_condense", "repair")

            cover_fit = await page_limit.fit_cover(
                stage["tailored"], profile, ledger, cache, job_snap,
                workdir / "cover", record=record_entailment,
                precondensed=stage.get("cover_condensed"),
                on_condensed=_on_condensed)
            stage["cover_fit"] = page_limit.fit_state(cover_fit)
            stage["entail_cache"] = cache
            _transition(package_id, stage=stage)

        _transition(package_id, "asserting", stage=stage)
        # A posting edited at any point before publish supersedes this
        # package (save_job_detail fails the row; this direct check is
        # the belt to that suspender — either way nothing stale ships).
        current_job = db.get_job(pkg["job_id"])
        if current_job is None or current_job.get("description_hash") != pkg["jd_hash"]:
            _transition(package_id, "failed",
                        error="superseded: posting changed before publish; "
                              "re-select the job to build a new package")
            return "failed"
        # Rebuild both PDFs from the persisted fitted state (pure
        # template render + local compile, zero LLM) — the same path
        # whether this process ran the fits or a previous one did — and
        # hard-assert (PLAN.md §4.5 step 4) so nothing unvalidated or
        # oversized can ever ship even if a fit loop regresses.
        resume_res = page_limit.compile_resume_state(
            stage["resume_fit"], profile, workdir / "resume")
        cover_res = page_limit.compile_cover_state(
            stage["cover_fit"], profile, job_snap, workdir / "cover")
        if resume_res.pages > limit or cover_res.pages != 1:
            raise page_limit.FitError(
                f"assertion failed: resume {resume_res.pages}/{limit} "
                f"pages, cover {cover_res.pages}/1 pages")
        if stage.get("validated_sha") != _content_sha(stage["tailored"]):
            raise page_limit.ValidationFailed([])
        # The final truth assertion binds to the SHIPPED documents: the
        # trimmed resume and the condensed cover letter, re-checked from
        # cached verdicts only (zero LLM — every claim was validated on
        # the way here, so a cache miss means drift and fails closed).
        shipped = json.loads(json.dumps(stage["resume_fit"]["tailored"]))
        shipped["cover_letter"] = stage["cover_fit"]["tailored"]["cover_letter"]
        failures = validate.cached_failures(shipped, profile, ledger,
                                            stage.get("entail_cache", {}))
        if failures:
            raise page_limit.ValidationFailed(failures)

        _transition(package_id, "publishing")
        final_dir = output_dir_for(job_snap, package_id)
        tmp_dir = final_dir.with_name(final_dir.name + ".publishing")
        # Both names are exclusive to THIS package id — anything already
        # there is this package's own interrupted publish, never another
        # package's artifacts.
        for leftover in (tmp_dir, final_dir):
            if leftover.exists():
                shutil.rmtree(leftover)
        tmp_dir.mkdir(parents=True)
        shutil.copy2(resume_res.pdf_path, tmp_dir / "resume.pdf")
        shutil.copy2(cover_res.pdf_path, tmp_dir / "cover_letter.pdf")
        gen = stage["generation"]
        (tmp_dir / "job.md").write_text(
            _job_md(job_snap, stage.get("jd_snapshot") or "", gen["score"]))
        tmp_dir.rename(final_dir)  # single-step publish into a fresh name
        shutil.rmtree(workdir, ignore_errors=True)

        # Provenance publishes from the per-stage snapshots (what each
        # LLM call actually used), never from completion-time state —
        # only the compile facts (engines, pages) and the template
        # versions come from this process, because this process really
        # did render and compile the shipped PDFs. llm_calls is the
        # append-only ledger verbatim: every call in the order it ran,
        # each attributed to the model/effort that actually made it —
        # including repeats (e.g. entailment before and after condense)
        # across engine recoveries.
        provenance = {
            "llm_calls": stage.get("llm_calls", []),
            "engine_resume": resume_res.engine,
            "engine_cover": cover_res.engine,
            "prompt_version": gen["prompt_version"],
            "schema_version": gen["schema_version"],
            "template_version": template.TEMPLATE_VERSION,
            "cover_template_version": cover_mod.TEMPLATE_VERSION,
            "profile_version": pkg["profile_version"],
            "jd_hash": pkg["jd_hash"],
            "page_limit": limit,
            "resume_pages": resume_res.pages,
            "cover_pages": cover_res.pages,
            "trimmed_bullets": len(stage["resume_fit"]["trimmed"]),
            "retry_count": db.get_package(package_id)["retry_count"],
        }
        _transition(package_id, "done", stage=stage, error=None,
                    provenance=provenance, output_dir=str(final_dir))
        return "done"

    except asyncio.CancelledError:
        raise  # shutdown: status stays mid-flight for startup recovery
    except Superseded:
        # A JD/profile change failed this row while we were driving it;
        # the invalidation already wrote the terminal state — hands off.
        log.info("package %s superseded mid-flight", package_id)
        return "superseded"
    except llm.UsageExhausted as exc:
        if not _park(package_id, "waiting_usage", stage, str(exc)):
            return "superseded"
        return "waiting_usage"
    except (llm.AuthRequired, llm.ModelNotEntitled, llm.ConfigError) as exc:
        # Human-actionable engine problem: nothing this package did wrong.
        # Park it queued with the reason; engine recovery resumes the batch.
        if not _park(package_id, "queued", stage,
                     f"{type(exc).__name__}: {exc}"):
            return "superseded"
        return "engine_blocked"
    except (llm.EngineError, latex.CompileError, page_limit.FitError,
            page_limit.ValidationFailed) as exc:
        if not _park(package_id, "failed", stage,
                     f"{type(exc).__name__}: {exc}"):
            return "superseded"
        return "failed"
    except Exception as exc:  # noqa: BLE001 — isolation: only this package fails
        log.exception("package %s failed unexpectedly", package_id)
        if not _park(package_id, "failed", stage,
                     f"{type(exc).__name__}: {exc}"):
            return "superseded"
        return "failed"


async def run_batch() -> str:
    """Process every resumable package in batch order. Returns why the
    batch stopped: 'idle' (nothing left to do), 'waiting_usage' (durable
    engine wait — caller arms the retry timer), 'engine_blocked' (caller
    resumes after engine recovery), or 'waiting_inputs' (every remaining
    package is queued behind scoring — its completion resumes them)."""
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
        all_waiting_inputs = True
        for pkg in rows:
            status = await run_package(pkg["id"])
            if status == "waiting_usage":
                return "waiting_usage"
            if status == "engine_blocked":
                return "engine_blocked"
            if status != "waiting_inputs":
                all_waiting_inputs = False
        if all_waiting_inputs:
            # Every remaining package is input-blocked and stays queued —
            # looping again would spin. Zero LLM was spent this pass.
            return "waiting_inputs"
        # Loop again: a package parked back to queued mid-list, or new
        # selections queued while we ran, get picked up before exiting.
