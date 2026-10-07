"""Postings that repeat one role (PLAN.md P8a): the rule, and the groups
the database keeps current from it. All postings here are made up; their
shapes are the ones real reposts take — the same text, the same text
re-spaced, a new requisition number, an edited repost — beside postings
that only share a name."""

import sqlite3

import pytest

from backend.core import db, duplicates

WORDS = [f"term{i}" for i in range(400)]
TEXT = " ".join(WORDS)


def _edited(start: int, length: int = 10, text_words=WORDS) -> str:
    """The text with `length` words from `start` replaced: one region of
    a posting rewritten."""
    words = list(text_words)
    words[start:start + length] = [f"new{start}_{i}" for i in range(length)]
    return " ".join(words)


def _posting(job_id, description=TEXT, *, title="Backend Engineer",
             company="Example Widgets Inc.", location="Toronto, ON",
             first_seen_at=None, description_hash=None):
    return {"job_id": job_id, "title": title, "company": company,
            "location": location, "description": description,
            "description_hash": description_hash or f"hash-{job_id}",
            "first_seen_at": first_seen_at or f"2026-08-{int(job_id[-1]) + 10:02d}"}


def _groups(postings):
    """The rule's answer as sets of job ids, groups of two or more only."""
    by_group: dict[str, set] = {}
    for job_id, group_id in duplicates.group(postings).items():
        by_group.setdefault(group_id, set()).add(job_id)
    return sorted(sorted(m) for m in by_group.values() if len(m) > 1)


# --- the rule ----------------------------------------------------------------

def test_reposts_are_one_role():
    respaced = TEXT.replace("term7 term8", "term7  ,term8")
    requisition = TEXT.replace("term200", "req0007")
    trimmed = " ".join(WORDS[:150] + WORDS[160:])  # a sentence dropped
    postings = [
        _posting("p1"),
        _posting("p2", description_hash="hash-p1"),  # the same text
        _posting("p3", respaced),
        _posting("p4", requisition),
        _posting("p5", trimmed),
    ]
    assert _groups(postings) == [["p1", "p2", "p3", "p4", "p5"]]


def test_a_shared_name_is_not_the_same_role():
    other_team = " ".join(f"other{i}" for i in range(400))
    assert _groups([_posting("p1"), _posting("p2", other_team)]) == []


def test_the_same_text_under_another_title_is_another_role():
    """One template, two roles: the title keeps them in separate buckets,
    which are never compared."""
    assert (duplicates.match_key(_posting("p1"))
            != duplicates.match_key(_posting("p2", title="Full Stack Engineer")))


def test_punctuation_in_a_title_is_kept():
    keys = {duplicates.match_key(_posting("p1", title=t)) for t in ("C", "C++", "C#")}
    assert len(keys) == 3
    # Case and spacing are not: these are the same title.
    assert (duplicates.match_key(_posting("p1", title="Backend  ENGINEER"))
            == duplicates.match_key(_posting("p2", title="backend engineer")))


def test_without_a_location_or_a_description_nothing_groups():
    assert duplicates.match_key(_posting("p1", location=None)) is None
    assert duplicates.match_key(_posting("p1", location="  ")) is None
    assert duplicates.match_key(_posting("p1", description=None)) is None
    # Two cities' openings of one role stay two.
    assert (duplicates.match_key(_posting("p1"))
            != duplicates.match_key(_posting("p2", location="Ottawa, ON")))


def test_a_chain_of_near_matches_does_not_join_its_ends():
    """A matches B and B matches C, but A does not match C: every pair in
    a group must match, so the oldest two group and C stands alone."""
    a = TEXT
    b = _edited(100)
    c = _edited(300, text_words=b.split())
    sa, sb, sc = (duplicates.shingles(t) for t in (a, b, c))
    assert duplicates.similarity(sa, sb) >= duplicates.SAME_TEXT
    assert duplicates.similarity(sb, sc) >= duplicates.SAME_TEXT
    assert duplicates.similarity(sa, sc) < duplicates.SAME_TEXT
    postings = [_posting("p1", a), _posting("p2", b), _posting("p3", c)]
    assert _groups(postings) == [["p1", "p2"]]
    # The order is the postings' age, not the order they are given in.
    assert _groups(list(reversed(postings))) == [["p1", "p2"]]


def test_a_group_is_named_by_its_oldest_posting():
    postings = [_posting("p2"), _posting("p1", description_hash="hash-p2")]
    assert set(duplicates.group(postings).values()) == {"p1"}


# --- the database keeps groups current --------------------------------------

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


def _card(job_id, *, title="Backend Engineer", location="Toronto, ON"):
    return {"job_id": job_id, "title": title, "company": "Example Widgets Inc.",
            "location": location, "posted_text": None, "workplace": "onsite",
            "easy_apply": False, "url": f"https://www.linkedin.com/jobs/view/{job_id}/"}


def _run(job_ids, **card):
    run_id = db.create_search_run({"keywords": "x", "cap": 50})
    db.record_search_page(run_id, [_card(j, **card) for j in job_ids],
                          next_start=len(job_ids), cap=50)
    return run_id


def _shared():
    """The database's groups, as sets of job ids."""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        rows = conn.execute("SELECT job_id, group_id FROM posting_groups").fetchall()
    finally:
        conn.close()
    return db._shared(dict(rows))


def _table(name):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(f"SELECT * FROM {name} ORDER BY 1, 2").fetchall()
    finally:
        conn.close()


def test_a_saved_description_groups_a_repost(test_db):
    first = _run(["1001", "1002"])
    assert db.save_job_detail("1001", TEXT, "hash-a").regrouped is False
    second = _run(["2001"])
    assert db.save_job_detail("2001", TEXT, "hash-a").regrouped is True
    assert _shared() == {frozenset({"1001", "2001"})}
    # Saving it again changes nothing, and says so.
    assert db.save_job_detail("2001", TEXT, "hash-a").regrouped is False

    groups = db.posting_groups(["2001", "1002"], second)
    assert set(groups) == {"2001"}
    assert groups["2001"]["id"] == "1001"
    assert [(m["job_id"], m["in_run"]) for m in groups["2001"]["members"]] == [
        ("1001", False), ("2001", True)]
    assert db.posting_groups(["1001"], first)["1001"]["members"][0]["in_run"] is True


def test_an_edit_that_rewrites_the_text_leaves_the_group(test_db):
    _run(["1001", "1002"])
    db.save_job_detail("1001", TEXT, "hash-a")
    db.save_job_detail("1002", TEXT, "hash-a")
    assert _shared() == {frozenset({"1001", "1002"})}
    other = " ".join(f"other{i}" for i in range(400))
    assert db.save_job_detail("1002", other, "hash-b").regrouped is True
    assert _shared() == set()


def test_a_card_that_renames_a_posting_moves_its_group(test_db):
    """A search card can change a known posting's title with no new
    description: the old bucket and the new one are both regrouped, in
    the page's own transaction."""
    _run(["1001", "1002", "1003"])
    _run(["1003"], title="Platform Engineer")
    for job_id in ("1001", "1002", "1003"):
        db.save_job_detail(job_id, TEXT, "hash-a")
    assert _shared() == {frozenset({"1001", "1002"})}

    _run(["1002"], title="Platform Engineer")
    assert _shared() == {frozenset({"1002", "1003"})}
    # The same through a single card.
    db.upsert_job(_card("1002"))
    assert _shared() == {frozenset({"1001", "1002"})}
    # A card that moves it to a place with no location ungroups it.
    _run(["1002"], location=None)
    assert _shared() == set()


def test_moving_a_groups_first_posting_regroups_the_rest(test_db):
    """The group's first posting leaves: the two left behind are regrouped
    under the next oldest, and no longer list the one that left."""
    _run(["1001", "1002", "1003"])
    for job_id in ("1001", "1002", "1003"):
        db.save_job_detail(job_id, TEXT, "hash-a")
    assert db.posting_groups(["1003"])["1003"]["id"] == "1001"
    _run(["1001"], title="Platform Engineer")
    assert _shared() == {frozenset({"1002", "1003"})}
    group = db.posting_groups(["1003"])["1003"]
    assert group["id"] == "1002"
    assert [m["job_id"] for m in group["members"]] == ["1002", "1003"]
    assert db.posting_groups(["1001"]) == {}


def test_an_older_posting_described_later_leads_its_group(test_db):
    _run(["1001", "1002", "1003"])
    db.save_job_detail("1002", TEXT, "hash-a")
    db.save_job_detail("1003", TEXT, "hash-a")
    assert db.posting_groups(["1003"])["1003"]["id"] == "1002"
    db.save_job_detail("1001", TEXT, "hash-a")
    assert db.posting_groups(["1003"])["1003"]["id"] == "1001"


def test_grouping_rewrites_nothing_else(test_db):
    run_id = _run(["1001", "1002"])
    for job_id in ("1001", "1002"):
        db.save_job_detail(job_id, f"Posting {job_id}", f"hash-{job_id}")
    package, _ = db.create_package("1001", 1, "hash-1001", 1, template_version=1,
                                   cover_template_version=1)
    before = {name: _table(name) for name in ("jobs", "search_run_jobs", "packages")}
    db.save_job_detail("1002", "Posting 1001", "hash-1001")
    assert _shared() == {frozenset({"1001", "1002"})}
    jobs = [row for row in _table("jobs") if row[0] != "1002"]
    assert jobs == [row for row in before["jobs"] if row[0] != "1002"]
    assert _table("search_run_jobs") == before["search_run_jobs"]
    assert _table("packages") == before["packages"]
    assert db.get_package(package)["job_id"] == "1001"
    assert {j["job_id"] for j in db.list_jobs(run_id)} == {"1001", "1002"}


def test_a_failed_regroup_saves_no_description(test_db, monkeypatch):
    """The description and its group commit together or not at all."""
    _run(["1001"])

    def broken(postings):
        raise RuntimeError("grouping failed")

    monkeypatch.setattr(duplicates, "group", broken)
    with pytest.raises(RuntimeError):
        db.save_job_detail("1001", TEXT, "hash-a")
    assert db.get_job("1001")["description"] is None


def _stale_groups():
    """Groups as an older rule left them: one wrong pair."""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("DELETE FROM posting_groups")
        conn.executemany(
            "INSERT INTO posting_groups VALUES (?, 'old-key', '1001', 0)",
            [("1001",), ("1003",)])
        conn.execute("UPDATE engine_state SET value = '0' WHERE key = ?",
                     (db._GROUPS_RULE_KEY,))
        conn.commit()
    finally:
        conn.close()


def test_a_new_rule_rebuilds_every_group_on_start(test_db):
    _run(["1001", "1002", "1003"])
    db.save_job_detail("1001", TEXT, "hash-a")
    db.save_job_detail("1002", TEXT, "hash-a")
    db.save_job_detail("1003", "Something else entirely", "hash-c")
    _stale_groups()
    before = {name: _table(name) for name in ("jobs", "search_run_jobs", "packages")}
    db.init()
    assert _shared() == {frozenset({"1001", "1002"})}
    assert {name: _table(name) for name in before} == before


def test_a_rebuild_that_fails_partway_changes_nothing(test_db, monkeypatch):
    _run(["1001", "1002", "1003", "1004"])
    db.save_job_detail("1001", TEXT, "hash-a")
    db.save_job_detail("1002", TEXT, "hash-a")
    _run(["1003", "1004"], title="Platform Engineer")
    db.save_job_detail("1003", TEXT, "hash-a")
    db.save_job_detail("1004", TEXT, "hash-a")
    _stale_groups()
    stale = _table("posting_groups")

    real, calls = duplicates.group, []

    def second_bucket_fails(postings):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("died partway")
        return real(postings)

    monkeypatch.setattr(duplicates, "group", second_bucket_fails)
    with pytest.raises(RuntimeError):
        db.init()
    assert len(calls) == 2  # the first bucket was written before it died
    assert _table("posting_groups") == stale
    monkeypatch.setattr(duplicates, "group", real)
    db.init()
    assert _shared() == {frozenset({"1001", "1002"}), frozenset({"1003", "1004"})}


async def test_the_job_list_names_each_postings_group(test_db):
    from backend.api import routes

    _run(["1001"])
    db.save_job_detail("1001", TEXT, "hash-a")
    package, _ = db.create_package("1001", 1, "hash-a", 1, template_version=1,
                                   cover_template_version=1)
    run_id = _run(["2001", "2002"])
    db.save_job_detail("2001", TEXT, "hash-a")
    db.save_job_detail("2002", "A role of its own.", "hash-b")
    jobs = {j["job_id"]: j for j in (await routes.jobs_list(run_id))["jobs"]}
    # Every posting is still listed under its own id.
    assert set(jobs) == {"2001", "2002"}
    assert jobs["2002"]["group"] is None
    assert jobs["2001"]["group"]["members"] == [
        {"job_id": "1001", "url": "https://www.linkedin.com/jobs/view/1001/",
         "first_seen_at": db.get_job("1001")["first_seen_at"], "in_run": False},
        {"job_id": "2001", "url": "https://www.linkedin.com/jobs/view/2001/",
         "first_seen_at": db.get_job("2001")["first_seen_at"], "in_run": True},
    ]
    assert db.get_package(package)["job_id"] == "1001"


async def test_a_fetch_that_regroups_announces_the_job_list(test_db, monkeypatch):
    from backend.core.events import broker
    from backend.sources.linkedin import job_detail

    page = ("<html><body><div id='JobDetails_AboutTheJob_1'><h2>About the job</h2>"
            f"<p>{TEXT}</p></div></body></html>")

    class FixtureBrowser:
        async def fetch_page(self, url, *, mode="results"):
            return page

    published = []
    monkeypatch.setattr(broker, "publish", lambda topic, data: published.append(topic))
    _run(["1001", "1002"])
    await job_detail.fetch_detail(FixtureBrowser(), "1001", force=True)
    assert published == []
    await job_detail.fetch_detail(FixtureBrowser(), "1002", force=True)
    assert published == ["jobs"]
