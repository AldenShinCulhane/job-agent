"""Automatic post-search scoring pipeline (PLAN.md §4.4) — no gate.

Per job in a run: ensure a cached description (paced browser fetch when
missing/stale), normalize it (LLM, cached by JD hash), score it
deterministically against the confirmed profile.

Stateless and idempotent: progress lives entirely in the data — cached
descriptions, requirements rows keyed by JD hash, score rows keyed by
hash + profile version — so the pipeline can be re-triggered at any
time (search completion, Connect recovery, cap reset, startup) and only
does the missing work. Session-level stops (auth, captcha, daily cap,
LLM usage exhausted) end the pass; per-job failures are isolated and
retried on the next trigger.
"""

import asyncio
import logging

from backend import sources
from backend.core import db, llm
from backend.core.events import broker
from backend.normalize import normalize_job
from backend.scoring.score import profile_years_experience, score_job

log = logging.getLogger(__name__)


class NoConfirmedProfile(Exception):
    pass


def confirmed_profile() -> tuple[int, dict]:
    latest = db.latest_profile()
    if latest is None or not latest[2]:
        raise NoConfirmedProfile("scoring needs a confirmed profile")
    return latest[0], latest[1]


def pending_count(run_id: int) -> int:
    """Remaining pipeline work for the run (0 = fully scored), or 0 when
    no profile is confirmed yet (nothing can be scored)."""
    try:
        version, _ = confirmed_profile()
    except NoConfirmedProfile:
        return 0
    return db.scoring_pending(run_id, version)


async def run_pipeline(run_id: int) -> dict:
    """One pass over the run's jobs. Returns a summary
    {scored, skipped, failed, stopped} where `stopped` names the
    session-level condition that ended the pass early (or None)."""
    version, profile = confirmed_profile()
    user_years = profile_years_experience(profile)
    scored = skipped = failed = 0
    stopped: str | None = None

    def profile_stale() -> bool:
        latest = db.latest_profile()
        return latest is None or not latest[2] or latest[0] != version

    jobs = db.list_jobs(run_id)
    total = len(jobs)
    for job in jobs:
        job_id = job["job_id"]
        try:
            # Generation token: the pass belongs to the profile version it
            # started with. Confirming a new profile cancels the worker,
            # but an in-flight iteration could still write a stale score
            # after an await — verify at iteration start AND again right
            # before the write below.
            if profile_stale():
                stopped = "stale_profile"
                log.info("scoring run %s stopped: profile changed mid-pass", run_id)
                break
            current = db.get_job(job_id)
            existing = db.get_score(job_id)
            if (
                existing is not None
                and existing["profile_version"] == version
                and current.get("description_hash")
                and existing["jd_hash"] == current["description_hash"]
            ):
                skipped += 1
                continue
            # fetch_detail is TTL/cache-aware: fresh descriptions cost no
            # browser action. Pace only around actual fetches.
            fetched = await sources.source.fetch_detail(job_id)
            if fetched.get("refetched"):
                await sources.page_delay()
            current = db.get_job(job_id)
            requirements = await normalize_job(job_id)
            result = score_job(requirements, profile, user_years=user_years)
            if profile_stale():  # the awaits above may have raced a confirm
                stopped = "stale_profile"
                log.info("scoring run %s stopped: profile changed mid-job", run_id)
                break
            db.save_score(
                job_id,
                current["description_hash"],
                version,
                result["deterministic_score"],
                result["breakdown"],
            )
            scored += 1
            broker.publish(
                "scoring",
                {"run_id": run_id, "done": scored + skipped, "total": total},
            )
        except asyncio.CancelledError:
            raise
        except (sources.AuthRequired, sources.CaptchaStop) as exc:
            stopped = "session"
            log.info("scoring run %s stopped: %s", run_id, exc)
            break
        except sources.DailyCapReached as exc:
            stopped = "cap"
            log.info("scoring run %s stopped at daily cap: %s", run_id, exc)
            break
        except llm.UsageExhausted as exc:
            stopped = "usage"
            log.info("scoring run %s stopped, usage exhausted: %s", run_id, exc)
            break
        except llm.EngineError as exc:
            # Auth/config/transient engine failures affect every job the
            # same way — stopping beats repeating the failure ~100 times.
            stopped = "engine"
            log.warning("scoring run %s stopped, engine error: %s", run_id, exc)
            break
        except Exception:  # noqa: BLE001 — isolate per-job failures
            failed += 1
            log.exception("scoring job %s failed; continuing", job_id)

    summary = {"scored": scored, "skipped": skipped, "failed": failed,
               "stopped": stopped}
    log.info("scoring run %s pass finished: %s", run_id, summary)
    return summary
