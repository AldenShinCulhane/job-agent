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
    {scored, skipped, failed, stopped, detail} where `stopped` names the
    session-level condition that ended the pass early (or None) and
    `detail` carries its human-readable cause."""
    version, profile = confirmed_profile()
    user_years = profile_years_experience(profile)
    scored = skipped = failed = 0
    stopped: str | None = None
    detail: str | None = None

    def profile_stale() -> bool:
        latest = db.latest_profile()
        return latest is None or not latest[2] or latest[0] != version

    async def process_job(job_id: str) -> str:
        """'scored' | 'skipped' | 'raced' | 'stale_profile'. The JD can be
        refreshed (user force-fetch) while normalization awaits — the
        hash is re-read before the write and a mismatch reprocesses the
        job once from the new text; a second race gives up ('raced', the
        retry timer revisits it)."""
        for _ in range(2):
            # Generation token: the pass belongs to the profile version
            # it started with. Confirming a new profile cancels the
            # worker, but an in-flight iteration could still write a
            # stale score after an await — verify at start AND right
            # before the write below.
            if profile_stale():
                return "stale_profile"
            current = db.get_job(job_id)
            existing = db.get_score(job_id)
            if (
                existing is not None
                and existing["profile_version"] == version
                and current.get("description_hash")
                and existing["jd_hash"] == current["description_hash"]
            ):
                return "skipped"
            # fetch_detail is TTL/cache-aware: fresh descriptions cost
            # no browser action. Pace only around actual fetches.
            fetched = await sources.source.fetch_detail(job_id)
            if fetched.get("refetched"):
                await sources.page_delay()
            current = db.get_job(job_id)
            requirements = await normalize_job(job_id)
            result = score_job(requirements, profile, user_years=user_years)
            if profile_stale():  # the awaits above may have raced a confirm
                return "stale_profile"
            fresh = db.get_job(job_id)
            if fresh.get("description_hash") != current.get("description_hash"):
                # JD refreshed mid-normalization: a score stamped with
                # the old hash would be invisible to the UI yet count as
                # pending forever. Recompute from the new text.
                log.info("job %s description moved mid-normalize; reprocessing",
                         job_id)
                continue
            db.save_score(
                job_id,
                current["description_hash"],
                version,
                result["deterministic_score"],
                result["breakdown"],
            )
            return "scored"
        return "raced"

    jobs = db.list_jobs(run_id)
    total = len(jobs)
    for job in jobs:
        job_id = job["job_id"]
        try:
            outcome = await process_job(job_id)
            if outcome == "scored":
                scored += 1
                broker.publish(
                    "scoring",
                    {"run_id": run_id, "done": scored + skipped, "total": total},
                )
            elif outcome == "skipped":
                skipped += 1
            elif outcome == "raced":
                failed += 1  # bounded — the retry timer revisits it
            else:  # stale_profile
                stopped = "stale_profile"
                log.info("scoring run %s stopped: profile changed mid-pass", run_id)
                break
        except asyncio.CancelledError:
            raise
        except (sources.AuthRequired, sources.CaptchaStop) as exc:
            stopped, detail = "session", str(exc)
            log.info("scoring run %s stopped: %s", run_id, exc)
            break
        except sources.DailyCapReached as exc:
            stopped, detail = "cap", str(exc)
            log.info("scoring run %s stopped at daily cap: %s", run_id, exc)
            break
        except llm.UsageExhausted as exc:
            stopped, detail = "usage", str(exc)
            log.info("scoring run %s stopped, usage exhausted: %s", run_id, exc)
            break
        except (llm.AuthRequired, llm.ModelNotEntitled, llm.ConfigError) as exc:
            # Human-actionable engine failures: retrying on a timer can't
            # fix a missing login or bad config — surface instead.
            stopped, detail = "engine_blocked", str(exc)
            log.warning("scoring run %s blocked on engine: %s", run_id, exc)
            break
        except llm.EngineError as exc:
            # Transient engine trouble affects every job the same way —
            # stopping beats repeating the failure ~100 times.
            stopped, detail = "engine", str(exc)
            log.warning("scoring run %s stopped, engine error: %s", run_id, exc)
            break
        except Exception:  # noqa: BLE001 — isolate per-job failures
            failed += 1
            log.exception("scoring job %s failed; continuing", job_id)

    summary = {"scored": scored, "skipped": skipped, "failed": failed,
               "stopped": stopped, "detail": detail}
    log.info("scoring run %s pass finished: %s", run_id, summary)
    return summary
