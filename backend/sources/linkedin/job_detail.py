"""Full job-description fetch per job, cached in SQLite by job id.

Cache policy (PLAN.md §4.3): `fetched_at` + content hash. A stale job
(older than the TTL) viewed again is silently re-fetched; an edited
posting is detected by hash change. Generated packages later pin the
exact description snapshot they were built from.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from bs4 import Comment, NavigableString

from backend.core import db
from backend.core.events import broker
from backend.sources.linkedin import selectors

log = logging.getLogger(__name__)

DETAIL_TTL = timedelta(days=3)


class DetailParseError(Exception):
    pass


def is_stale(job: dict, now: datetime | None = None) -> bool:
    """True when the cached description is missing or older than the TTL."""
    if not job.get("description") or not job.get("detail_fetched_at"):
        return True
    now = now or datetime.now(timezone.utc)
    fetched = datetime.fromisoformat(job["detail_fetched_at"])
    return now - fetched > DETAIL_TTL


def canonical_text(description: str) -> str:
    """The description with every run of whitespace collapsed to one
    space — the form a posting is identified by."""
    return " ".join(description.split())


def description_hash(description: str) -> str:
    """Hash of the canonical text, so line breaks never move it. Earlier
    revisions stored the canonical text itself and hashed that, so every
    hash already stored is what this gives for the same posting."""
    return hashlib.sha256(canonical_text(description).encode()).hexdigest()[:16]


# Where a posting's markup starts a new paragraph or a new line. Anything
# else — a span, a link, bold text — joins its words to the line it is on.
_PARAGRAPH_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
})
_LINE_TAGS = frozenset({"br", "li", "tr"})
_SPACE, _LINE, _PARAGRAPH = 0, 1, 2
_SEPARATORS = {_SPACE: " ", _LINE: "\n", _PARAGRAPH: "\n\n"}


def readable_text(node) -> str | None:
    """The description with its paragraphs and lines kept.

    Built from exactly the strings `selectors.text_of` joins, in the same
    order: only the whitespace between two words changes, a line or
    paragraph break where the markup puts one. Its canonical text is
    therefore always the text earlier revisions stored, so a posting's
    hash holds across this change and across any markup-only edit.
    """
    if node is None:
        return None
    wanted = node.interesting_string_types or node.MAIN_CONTENT_STRING_TYPES
    if isinstance(wanted, type):
        wanted = {wanted}
    words: list[str] = []
    pending = _SPACE

    def walk(tag) -> None:
        nonlocal pending
        for child in tag.children:
            if isinstance(child, NavigableString):
                # The same strings get_text keeps: no comments, no script.
                if type(child) not in wanted:
                    continue
                for word in child.split():
                    if words:
                        words.append(_SEPARATORS[pending])
                    words.append(word)
                    pending = _SPACE
                continue
            name = child.name
            brk = (_PARAGRAPH if name in _PARAGRAPH_TAGS
                   else _LINE if name in _LINE_TAGS else _SPACE)
            pending = max(pending, brk)
            walk(child)
            pending = max(pending, brk)

    walk(node)
    text = "".join(words)
    if not text:
        return None
    # True by construction, and checked anyway because every stored hash
    # rests on it: should it ever fail, the posting keeps its identity and
    # loses only its line breaks.
    if canonical_text(text) != selectors.text_of(node):
        return selectors.text_of(node)
    return text


def parse_detail(html: str) -> str:
    """Detail-page HTML -> description text with its paragraph and line
    breaks (see `readable_text`)."""
    soup = selectors.parse_html(html)
    node = selectors.select_first(soup, "detail_description")
    text = readable_text(node)
    if text:
        return text
    # Selector-free fallback: LinkedIn's classes are obfuscated and churn,
    # but the description section is headed "About the job". Climb from
    # that anchor to the first ancestor with substantial text.
    anchor = soup.find(
        # Real text only — bs4 string search also matches HTML comments.
        string=lambda s: s and "About the job" in s and not isinstance(s, Comment)
    )
    if anchor is not None:
        el = anchor.parent
        while el is not None and el.name not in ("body", "html", "[document]"):
            text = selectors.text_of(el)
            if text and len(text) > 200:
                return readable_text(el)
            el = el.parent
    raise DetailParseError("no job description found on the page")


async def fetch_detail(supervisor, job_id: str, force: bool = False) -> dict:
    """Return the job with a fresh-enough description, re-fetching when
    stale (TTL) or forced. Reports `changed` when the posting's content
    hash moved — edited postings invalidate downstream caches (M3)."""
    job = db.get_job(job_id)
    if job is None:
        raise KeyError(f"unknown job {job_id}")
    if not force and not is_stale(job):
        return {**job, "refetched": False, "changed": False}

    html = await supervisor.fetch_page(job["url"], mode="detail")
    try:
        description = parse_detail(html)
    except DetailParseError:
        # Keep the unparseable page locally (gitignored .data/) so the
        # selector chain can be fixed against the real DOM instead of
        # guesswork. Overwritten each failure; never leaves the machine.
        dump = db.DATA_DIR / "debug_last_detail.html"
        dump.write_text(html)
        log.warning("job %s: no description matched; page dumped to %s", job_id, dump)
        raise
    new_hash = description_hash(description)
    changed = bool(job["description_hash"]) and new_hash != job["description_hash"]
    if changed:
        log.info("job %s description changed (hash %s -> %s)",
                 job_id, job["description_hash"], new_hash)
        # Hash moved -> normalized requirements/score/explanation are all
        # stale; drop them so the pipeline recomputes (PLAN.md §4.4).
        db.invalidate_derived(job_id)
    saved = db.save_job_detail(job_id, description, new_hash)
    if saved.superseded:
        broker.publish("packages", {"event": "superseded", "ids": saved.superseded})
    if saved.regrouped:
        # The text decides which postings repeat a role: the job list's
        # rows changed even where no score did.
        broker.publish("jobs", {"event": "regrouped", "job_id": job_id})
    return {**db.get_job(job_id), "refetched": True, "changed": changed}
