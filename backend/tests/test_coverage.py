"""Keyword coverage of a finished package (PLAN.md P8c): which of the
posting's skills the package's shipped documents mention, and where. The
prose matcher against labelled cases, the inputs it must read (the
generation's requirements, the pinned profile, the fitted resume and the
condensed letter), and no coverage, never an empty one, when one is gone.
All data fake."""

import copy

import pytest

from backend.core import db
from backend.normalize.requirements import cache_key
from backend.scoring.score import canonical_skill
from backend.tailor import coverage


def _mentioned(skill: str, text: str) -> bool | None:
    return coverage.mentioned(canonical_skill(skill), [text])


@pytest.mark.parametrize("skill, text, expected", [
    # Java is not JavaScript, either way round.
    ("Java", "Built services in Java and Kotlin", True),
    ("Java", "Wrote JavaScript for the dashboard", False),
    ("JavaScript", "Built services in Java", False),
    ("Java", "Built Java-based tools", True),
    ("Java", "Built Spring Boot (Java) integrations.", True),
    # Never inside a longer name.
    ("SQL", "Tuned MySQL and PostgreSQL", False),
    ("SQL", "Wrote SQL migrations", True),
    ("React", "Built reactive pipelines", False),
    ("C++", "Wrote C drivers", False),
    ("C++", "Wrote C++ drivers", True),
    ("C#", "Built C# services", True),
    ("Objective-C", "Shipped Objective-C apps", True),
    ("Objective-C", "Bridged it in Objective-C++", False),
    # By the scorer's aliases, either way round.
    ("PostgreSQL", "Ran Postgres in production", True),
    ("K8s", "Deployed on Kubernetes", True),
    ("Kubernetes", "Deployed on k8s clusters", True),
    ("Amazon Web Services", "Hosted on AWS", True),
    ("JS", "Wrote JavaScript daily", True),
    # A two-letter spelling isn't searched for: TS, PY and TF read as
    # other things.
    ("JavaScript", "Wrote JS daily", False),
    # Spellings that are everyday words are not searched for; the
    # skill's other spellings are.
    ("Next.js", "Planned the next release", False),
    ("Next.js", "Built a Next.js dashboard", True),
    ("Node.js", "Each node in the graph", False),
    ("Node.js", "FastAPI/Node.js backends", True),
    ("REST", "Mentored the rest of the team", False),
    ("REST", "Designed RESTful services", True),
    ("REST APIs", "Built REST APIs", True),
    # Plurals, case and line breaks.
    ("Microservice", "Split it into microservices", True),
    ("Machine learning", "Applied MACHINE\nlearning", True),
    ("CI/CD", "Ran CI/CD on GitHub Actions", True),
])
def test_prose_matches_whole_names_and_aliases(skill, text, expected):
    assert _mentioned(skill, text) is expected


@pytest.mark.parametrize("skill", ["Go", "Golang", "C", "R", "AI", "ML"])
def test_short_names_are_never_looked_for_in_prose(skill):
    """Go, C, R and any name of two characters or fewer match in the
    lists only: in prose they read as other words."""
    assert _mentioned(skill, "Go build it in C and R with AI and ML, or in Golang") is None


PROFILE = {
    "name": "Casey Example",
    "headline": "Software Engineer",
    "contact": {"phone": None, "email": "casey@example.com", "linkedin": None,
                "github": None, "website": None, "location": "Toronto, ON"},
    "experiences": [
        {"title": "Software Engineer", "company": "Example Widgets Inc.",
         "location": None, "start": "Jun 2024", "end": "Present",
         "bullets": ["Built internal tools with Python",
                     "Streamed events through Kafka"]},
        {"title": "Developer", "company": "Sample Systems & Co",
         "location": None, "start": "Jan 2023", "end": "May 2024",
         "bullets": ["Indexed listings in Elasticsearch"]},
    ],
    "projects": [
        {"name": "Sample Project", "technologies": ["Kubernetes"], "date": None,
         "link": None, "bullets": ["Wrote a CLI tool"]},
    ],
    "education": [
        {"institution": "Example University", "location": None,
         "degree": "BSc Computer Science", "start": None, "end": "2024",
         "details": ["Coursework in relational databases"]},
    ],
    "skills": [
        {"category": "Languages", "items": ["Python", "Go"]},
        {"category": "Tools", "items": ["Docker"]},
    ],
}

REQUIREMENTS = {
    "required_skills": ["Python", "Go", " Kafka ", "Kubernetes", "Java", "python ",
                        "Relational databases", "GraphQL"],
    "preferred_skills": ["Docker", "Terraform", "Elasticsearch", "Python", "Rust", "CLI"],
    "min_years_experience": 2, "education_requirement": "bachelor",
}


def _stage(requirements=REQUIREMENTS):
    """A finished package's stage: the draft generation wrote, the resume
    fitted from it (Kafka's bullet trimmed to fit), and the letter
    condensed from the draft's (Java dropped)."""
    draft = {
        "headline": {"text": "Backend Engineer | GraphQL", "source_ids": []},
        "section_order": ["experience", "projects", "skills", "education"],
        "skills_order": ["Tools", "Languages"],
        # The second experience isn't tailored: it ships its own bullets.
        "experiences": [{"index": 0, "bullets": [
            {"text": "Built internal tools with Python", "source_ids": [], "relevance": 90},
            {"text": "Streamed events through Kafka", "source_ids": [], "relevance": 10},
        ]}],
        "projects": [{"index": 0, "bullets": [
            {"text": "Wrote a CLI tool", "source_ids": [], "relevance": 40},
        ]}],
        "cover_letter": {"paragraphs": [
            {"text": "I build backend tools in Python and Java.", "source_ids": []},
            {"text": "I would bring Terraform to your platform team.", "source_ids": []},
        ]},
    }
    fitted = copy.deepcopy(draft)
    fitted["experiences"][0]["bullets"].pop()
    condensed = {"paragraphs": [
        {"text": "I build backend tools in Python.", "source_ids": []},
        {"text": "I would bring Terraform to your platform team.", "source_ids": []},
    ]}
    return {
        "tailored": draft,
        "generation": {"prompt_version": 1, "schema_version": 1,
                       "requirements": requirements,
                       "score": {"deterministic_score": 70.0, "breakdown": {}}},
        "resume_fit": {"tailored": fitted, "layout": {}, "trimmed": [], "condensed": False},
        "cover_fit": {"tailored": {**fitted, "cover_letter": condensed}, "layout": {},
                      "trimmed": [], "condensed": True},
    }


def _package(stage=None, status="done"):
    return {"id": 1, "status": status, "profile_version": 1,
            "stage": _stage() if stage is None else stage}


def _where(found):
    return {(s["kind"], s["label"]): (s["listed"], s["resume"], s["letter"]) for s in found}


def test_coverage_reads_the_shipped_documents():
    found = coverage.package_coverage(_package(), PROFILE)
    assert [(s["kind"], s["label"]) for s in found] == [
        # Deduplicated per kind, in the posting's words, as the
        # checklist lists them.
        ("required", "Python"), ("required", "Go"), ("required", "Kafka"),
        ("required", "Kubernetes"), ("required", "Java"),
        ("required", "Relational databases"), ("required", "GraphQL"),
        ("preferred", "Docker"), ("preferred", "Terraform"),
        ("preferred", "Elasticsearch"), ("preferred", "Python"), ("preferred", "Rust"),
        ("preferred", "CLI"),
    ]
    assert _where(found) == {
        ("required", "Python"): (True, True, True),
        # Listed; the prose isn't searched for a short name.
        ("required", "Go"): (True, None, None),
        # Its bullet was trimmed to fit: the draft's mention never shipped.
        ("required", "Kafka"): (False, False, False),
        # A project's technologies are a list.
        ("required", "Kubernetes"): (True, False, False),
        # The draft letter's mention was condensed away.
        ("required", "Java"): (False, False, False),
        # Education details are bullets on the resume.
        ("required", "Relational databases"): (False, True, False),
        # The tailored headline.
        ("required", "GraphQL"): (False, True, False),
        ("preferred", "Docker"): (True, False, False),
        ("preferred", "Terraform"): (False, False, True),
        # An experience the tailoring left alone ships its own bullets.
        ("preferred", "Elasticsearch"): (False, True, False),
        ("preferred", "Python"): (True, True, True),
        ("preferred", "Rust"): (False, False, False),
        # A project's bullet.
        ("preferred", "CLI"): (False, True, False),
    }


def test_lists_match_by_the_scorers_rules():
    """A listed skill is one the checklist marks met: aliases and the
    scorer's tolerance for plural drift, never a substring."""
    profile = copy.deepcopy(PROFILE)
    profile["skills"] = [{"category": "Tools", "items": ["Postgres", "Microservices",
                                                          "JavaScript"]}]
    requirements = {"required_skills": ["PostgreSQL", "Microservice", "Java"],
                    "preferred_skills": []}
    found = coverage.package_coverage(_package(_stage(requirements)), profile)
    assert [s["listed"] for s in found] == [True, True, False]


@pytest.mark.parametrize("gone", [
    "generation", "requirements", "resume_fit", "cover_fit", "cover_letter", "paragraphs",
])
def test_a_package_missing_an_input_has_no_coverage(gone):
    """None, never an empty list: an empty one would read as a posting
    that names no skills."""
    stage = _stage()
    if gone == "requirements":
        stage["generation"]["requirements"] = None
    elif gone == "cover_letter":
        del stage["cover_fit"]["tailored"]["cover_letter"]
    elif gone == "paragraphs":
        del stage["cover_fit"]["tailored"]["cover_letter"]["paragraphs"]
    else:
        del stage[gone]
    assert coverage.package_coverage(_package(stage), PROFILE) is None


def test_no_coverage_without_the_profile_or_before_the_package_finishes():
    assert coverage.package_coverage(_package(), None) is None
    for status in ("queued", "generating", "fitting", "failed", "waiting_usage"):
        assert coverage.package_coverage(_package(status=status), PROFILE) is None


def test_a_posting_that_names_no_skills_has_empty_coverage():
    stage = _stage({"required_skills": [], "preferred_skills": []})
    assert coverage.package_coverage(_package(stage), PROFILE) == []


# --- /api/packages ------------------------------------------------------------

@pytest.fixture
def test_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init()
    return db


async def _listed():
    from backend.api import routes
    answer = await routes.packages_list()
    return {p["id"]: p for p in answer["packages"]}


def _seed_package(job_id, version, stage, status="done"):
    db.upsert_job({
        "job_id": job_id, "title": "Backend Engineer", "company": "Example Widgets Inc.",
        "location": "Toronto, ON", "posted_text": None, "workplace": None,
        "easy_apply": False, "url": f"https://example.invalid/jobs/{job_id}",
    })
    db.save_job_detail(job_id, "We need Python.", f"hash-{job_id}")
    package, _ = db.create_package(job_id, version, f"hash-{job_id}", 1, template_version=1,
                                   cover_template_version=1)
    db.update_package(package, status=status, stage=stage)
    return package


@pytest.mark.anyio
async def test_packages_carry_coverage_from_their_own_inputs(test_db):
    """The requirements the generation used, not the cache a re-score
    replaced; the profile version the package is pinned to, not the
    newest one."""
    pinned = db.save_profile(PROFILE)
    newer = copy.deepcopy(PROFILE)
    newer["skills"] = [{"category": "Languages", "items": ["Rust"]}]
    db.save_profile(newer)
    done = _seed_package("2001", pinned, _stage())
    db.save_requirements(cache_key("hash-2001"),
                         {"required_skills": ["COBOL"], "preferred_skills": []})
    failed = _seed_package("2002", pinned, _stage(), status="failed")
    gone = _seed_package("2003", pinned, {"tailored": _stage()["tailored"]})
    listed = await _listed()
    found = _where(listed[done]["coverage"])
    assert ("required", "COBOL") not in found
    assert found[("required", "Python")] == (True, True, True)
    assert found[("preferred", "Rust")] == (False, False, False)
    assert listed[failed]["coverage"] is None
    assert listed[gone]["coverage"] is None


@pytest.mark.anyio
async def test_a_package_whose_profile_version_is_gone_has_no_coverage(test_db):
    done = _seed_package("2004", 99, _stage())
    assert (await _listed())[done]["coverage"] is None
