"""
Automated test harness for the job search pipeline.

Tests resume tailoring, cover letter generation, and page limit enforcement
using mock job data and test profiles. Requires a configured LLM API key.

Usage:
    uv run python tools/test_pipeline.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# ── Mock Data ──────────────────────────────────────────────────────────

MOCK_JOBS = [
    {
        "title": "Frontend Developer",
        "company": "TestCorp Alpha",
        "location": "Toronto, ON",
        "experience_level": "Mid Level",
        "skills": ["react", "typescript", "css", "html", "javascript"],
        "requirements_summary": "3+ years React experience, TypeScript required",
        "description_text": "We are looking for a frontend developer proficient in React and TypeScript to build modern web applications.",
        "match_score": 95.0,
        "match_breakdown": {"skills": 90, "experience": 95, "education": 100},
        "match_reasons": ["Strong React skills"],
        "gap_reasons": [],
        "matched_skills": ["react", "typescript", "javascript", "html", "css"],
        "missing_skills": ["graphql"],
        "analysis": {
            "required_skills": ["React", "TypeScript", "CSS"],
            "preferred_skills": ["GraphQL", "Next.js"],
            "years_experience_required": 3,
            "education_requirement": "Bachelor's in CS",
            "company_type": "startup",
            "role_summary": "Build and maintain React-based web applications with TypeScript.",
            "key_responsibilities": ["Build UI components", "Write unit tests", "Code reviews"],
            "red_flags": [],
            "culture_signals": ["remote-first", "collaborative"],
        },
    },
    {
        "title": "Backend Engineer",
        "company": "TestCorp Beta",
        "location": "Vancouver, BC",
        "experience_level": "Mid Level",
        "skills": ["python", "django", "postgresql", "docker", "aws"],
        "requirements_summary": "Python/Django backend development, cloud experience preferred",
        "description_text": "Backend engineer to design and build scalable APIs using Python and Django.",
        "match_score": 80.0,
        "match_breakdown": {"skills": 70, "experience": 85, "education": 100},
        "match_reasons": ["Python experience"],
        "gap_reasons": ["Limited Django experience"],
        "matched_skills": ["python", "sql", "docker"],
        "missing_skills": ["django", "aws"],
        "analysis": {
            "required_skills": ["Python", "Django", "PostgreSQL"],
            "preferred_skills": ["Docker", "AWS", "Redis"],
            "years_experience_required": 3,
            "education_requirement": "Bachelor's in CS",
            "company_type": "enterprise",
            "role_summary": "Design and build scalable REST APIs using Python and Django.",
            "key_responsibilities": ["API development", "Database design", "CI/CD"],
            "red_flags": [],
            "culture_signals": ["fast-paced", "agile"],
        },
    },
    {
        "title": "Full Stack Developer",
        "company": "TestCorp Gamma",
        "location": "Remote",
        "experience_level": "Entry Level",
        "skills": ["javascript", "react", "node.js", "sql", "git"],
        "requirements_summary": "JavaScript full-stack developer, React + Node.js",
        "description_text": "Full stack developer for web applications using React frontend and Node.js backend.",
        "match_score": 90.0,
        "match_breakdown": {"skills": 85, "experience": 90, "education": 100},
        "match_reasons": ["Full-stack match"],
        "gap_reasons": [],
        "matched_skills": ["javascript", "react", "sql", "git"],
        "missing_skills": ["node.js"],
        "analysis": {
            "required_skills": ["JavaScript", "React", "Node.js", "SQL"],
            "preferred_skills": ["TypeScript", "MongoDB"],
            "years_experience_required": 1,
            "education_requirement": None,
            "company_type": "startup",
            "role_summary": "Build full-stack web applications with React and Node.js.",
            "key_responsibilities": ["Frontend development", "API development", "Database queries"],
            "red_flags": [],
            "culture_signals": ["remote-first", "startup culture"],
        },
    },
]

MOCK_PROFILE_FRONTEND = {
    "personal": {
        "name": "Test User Alpha",
        "email": "alpha@test.com",
        "phone": "555-0100",
        "location": "Toronto, ON",
        "linkedin": "https://linkedin.com/in/testalpha",
        "github": "https://github.com/testalpha",
    },
    "summary": "Frontend developer with 2 years of experience in React and TypeScript.",
    "skills": {
        "programming_languages": ["JavaScript", "TypeScript", "Python", "HTML", "CSS"],
        "frameworks": ["React", "Angular", "Bootstrap", "jQuery"],
        "tools": ["Git", "VS Code", "Docker", "Figma"],
    },
    "experience": {
        "total_years": 2,
        "positions": [
            {
                "title": "Frontend Developer",
                "company": "Web Agency Inc",
                "location": "Toronto, ON",
                "start_date": "2023-06",
                "end_date": "present",
                "highlights": [
                    "Built responsive web applications using React and TypeScript serving 10K daily users",
                    "Implemented component library reducing development time by 30%",
                ],
            },
            {
                "title": "Junior Developer",
                "company": "Startup Co",
                "location": "Toronto, ON",
                "start_date": "2022-01",
                "end_date": "2023-05",
                "highlights": [
                    "Developed frontend features using JavaScript and React",
                    "Collaborated with design team to implement pixel-perfect UIs",
                    "Wrote unit tests with Jest achieving 80% code coverage",
                ],
            },
        ],
    },
    "education": [
        {
            "degree": "Bachelor of Science in Computer Science",
            "field": "Computer Science",
            "institution": "University of Toronto",
            "graduation_year": 2022,
        }
    ],
    "projects": [
        {
            "name": "Portfolio Website",
            "start_date": "2022-01",
            "end_date": "2022-03",
            "technologies": ["React", "TypeScript", "Tailwind CSS"],
            "highlights": [
                "Built a responsive portfolio site with dark mode toggle",
                "Deployed on Vercel with CI/CD pipeline",
            ],
        }
    ],
}

MOCK_PROFILE_BACKEND = {
    "personal": {
        "name": "Test User Beta",
        "email": "beta@test.com",
        "phone": "555-0200",
        "location": "Vancouver, BC",
        "linkedin": "https://linkedin.com/in/testbeta",
    },
    "summary": "Backend developer specializing in Python and database systems.",
    "skills": {
        "programming_languages": ["Python", "Java", "SQL", "Go"],
        "frameworks": ["Django", "Flask", "Spring Boot"],
        "tools": ["Docker", "PostgreSQL", "Redis", "AWS"],
    },
    "experience": {
        "total_years": 3,
        "positions": [
            {
                "title": "Backend Developer",
                "company": "Data Systems Corp",
                "location": "Vancouver, BC",
                "start_date": "2022-06",
                "end_date": "present",
                "highlights": [
                    "Designed RESTful APIs serving 50K requests per day using Django",
                    "Optimized PostgreSQL queries reducing response time by 40%",
                    "Implemented caching layer with Redis improving throughput by 3x",
                ],
            },
        ],
    },
    "education": [
        {
            "degree": "Bachelor of Science in Computer Science",
            "field": "Computer Science",
            "institution": "UBC",
            "graduation_year": 2022,
        }
    ],
    "projects": [
        {
            "name": "API Gateway",
            "start_date": "2021-09",
            "end_date": "2022-01",
            "technologies": ["Python", "FastAPI", "Docker"],
            "highlights": [
                "Built a rate-limiting API gateway handling 1K requests/sec",
                "Containerized with Docker and deployed on AWS ECS",
            ],
        }
    ],
}


# ── Test Helpers ───────────────────────────────────────────────────────

def _setup_tmp(jobs, profile, tmp_dir):
    """Write mock data to tmp_dir for pipeline tools to consume."""
    os.makedirs(tmp_dir, exist_ok=True)

    jobs_path = os.path.join(tmp_dir, "test_jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)

    profile_path = os.path.join(tmp_dir, "test_profile.yaml")
    import yaml
    with open(profile_path, "w", encoding="utf-8") as f:
        yaml.dump(profile, f, default_flow_style=False, allow_unicode=True)

    return jobs_path, profile_path


def _print_result(test_name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    msg = f"  [{status}] {test_name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return passed


# ── Tests ──────────────────────────────────────────────────────────────

def test_llm_connection():
    """Test that the configured LLM provider responds."""
    from llm_client import check_llm, chat_completion, get_provider_name

    if not check_llm():
        return _print_result("LLM connection", False, "No API key configured")

    try:
        response = chat_completion(
            system="Reply with exactly: OK",
            user_message="Test",
            max_tokens=100,
        )
        return _print_result(
            "LLM connection",
            "ok" in response.lower(),
            f"{get_provider_name()} responded: {response[:50]}",
        )
    except Exception as e:
        return _print_result("LLM connection", False, str(e))


def test_resume_tailoring():
    """Test that bullet tailoring produces different output for different jobs."""
    from llm_client import check_llm
    if not check_llm():
        return _print_result("Resume tailoring", False, "No API key")

    from generate_documents import generate_tailored_bullets
    import time

    profile = MOCK_PROFILE_FRONTEND
    job_frontend = MOCK_JOBS[0]  # Frontend Developer
    job_backend = MOCK_JOBS[1]   # Backend Engineer

    print("    Tailoring for frontend job...")
    result_frontend = generate_tailored_bullets(job_frontend, profile)
    from llm_client import get_call_delay
    time.sleep(get_call_delay())
    print("    Tailoring for backend job...")
    result_backend = generate_tailored_bullets(job_backend, profile)

    if result_frontend is None and result_backend is None:
        return _print_result("Resume tailoring", False, "Both calls returned None")

    if result_frontend is None or result_backend is None:
        return _print_result(
            "Resume tailoring", False,
            f"Frontend={'OK' if result_frontend else 'None'}, Backend={'OK' if result_backend else 'None'}",
        )

    # Check that the tailored bullets are actually different
    fe_text = json.dumps(result_frontend)
    be_text = json.dumps(result_backend)
    different = fe_text != be_text

    return _print_result(
        "Resume tailoring",
        different,
        f"Frontend and backend bullets are {'different' if different else 'IDENTICAL (not tailored)'}",
    )


def test_resume_one_page():
    """Test that generated resumes are exactly 1 page."""
    from llm_client import check_llm
    if not check_llm():
        return _print_result("Resume 1-page", False, "No API key")

    from generate_documents import (
        generate_tailored_bullets, build_resume_tex, compile_tex_to_pdf,
        enforce_one_page, _find_pdflatex, _get_pdf_page_count,
    )
    import time

    if not _find_pdflatex():
        return _print_result("Resume 1-page", False, "pdflatex not installed")

    profile = MOCK_PROFILE_FRONTEND
    job = MOCK_JOBS[0]

    print("    Generating tailored resume...")
    tailored = generate_tailored_bullets(job, profile)
    tex = build_resume_tex(profile, tailored)

    with tempfile.TemporaryDirectory() as tmp:
        tex_path = os.path.join(tmp, "resume.tex")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex)

        if not compile_tex_to_pdf(tex_path):
            return _print_result("Resume 1-page", False, "pdflatex compilation failed")

        pdf_path = tex_path.replace(".tex", ".pdf")
        pages = _get_pdf_page_count(pdf_path)

        if pages > 1:
            enforce_one_page(tex_path, profile, tailored)
            pages = _get_pdf_page_count(pdf_path)

        return _print_result(
            "Resume 1-page",
            pages == 1,
            f"{pages} page(s)",
        )


def test_cover_letter_generation():
    """Test that cover letters generate and fit within character budget."""
    from llm_client import check_llm
    if not check_llm():
        return _print_result("Cover letter", False, "No API key")

    from generate_documents import generate_cover_letter, _trim_cover_letter, COVER_LETTER_MAX_CHARS

    profile = MOCK_PROFILE_FRONTEND
    job = MOCK_JOBS[0]

    # Build base resume text
    from generate_documents import _profile_to_resume_text
    base_resume = _profile_to_resume_text(profile)

    print("    Generating cover letter...")
    letter = generate_cover_letter(job, profile, base_resume)
    if not letter:
        return _print_result("Cover letter", False, "Generation returned empty")

    letter = _trim_cover_letter(letter)
    chars = len(letter)
    within_budget = chars <= COVER_LETTER_MAX_CHARS + 200  # small tolerance

    return _print_result(
        "Cover letter",
        within_budget,
        f"{chars} chars (budget: ~{COVER_LETTER_MAX_CHARS})",
    )


def test_different_profiles():
    """Test that different profiles produce different resumes for the same job."""
    from llm_client import check_llm
    if not check_llm():
        return _print_result("Profile differentiation", False, "No API key")

    from generate_documents import generate_tailored_bullets, build_resume_tex
    import time

    job = MOCK_JOBS[2]  # Full Stack Developer

    print("    Tailoring for frontend profile...")
    result_fe = generate_tailored_bullets(job, MOCK_PROFILE_FRONTEND)
    tex_fe = build_resume_tex(MOCK_PROFILE_FRONTEND, result_fe)

    from llm_client import get_call_delay
    time.sleep(get_call_delay())

    print("    Tailoring for backend profile...")
    result_be = generate_tailored_bullets(job, MOCK_PROFILE_BACKEND)
    tex_be = build_resume_tex(MOCK_PROFILE_BACKEND, result_be)

    # They should be different because different profiles have different content
    different = tex_fe != tex_be

    return _print_result(
        "Profile differentiation",
        different,
        f"Resumes are {'different' if different else 'IDENTICAL (unexpected)'}",
    )


def test_resume_upload_mode():
    """Test that cover letters can be generated from a plain-text resume (no profile)."""
    from llm_client import check_llm
    if not check_llm():
        return _print_result("Resume upload mode", False, "No API key")

    from generate_documents import generate_cover_letter

    # Simulate a plain-text resume
    base_resume = """Test User
test@example.com | 555-0000 | Toronto, ON

EXPERIENCE
Frontend Developer at Web Agency (2023 - present)
- Built React applications serving 10K daily users
- Implemented CI/CD pipeline with GitHub Actions

EDUCATION
BSc Computer Science, University of Toronto (2022)

SKILLS: JavaScript, TypeScript, React, Python, SQL, Git"""

    job = MOCK_JOBS[0]
    empty_profile = {"personal": {"name": "Test User"}}

    print("    Generating cover letter from plain-text resume...")
    letter = generate_cover_letter(job, empty_profile, base_resume)

    return _print_result(
        "Resume upload mode",
        bool(letter) and len(letter) > 100,
        f"Generated {len(letter)} chars" if letter else "Empty response",
    )


# ── Main ───────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Pipeline Test Harness")
    print("=" * 55)

    from llm_client import get_provider_name
    print(f"\n  Provider: {get_provider_name()}")
    print()

    results = []

    # Test 1: LLM connection
    results.append(test_llm_connection())

    if not results[0]:
        print("\n  LLM connection failed — skipping remaining tests.")
        print("  Add at least one LLM API key to .env (get a free SambaNova key at https://cloud.sambanova.ai/apis).")
        sys.exit(1)

    import time
    from llm_client import get_call_delay
    delay = get_call_delay()

    # Test 2: Resume tailoring
    time.sleep(delay)
    results.append(test_resume_tailoring())

    # Test 3: Resume 1-page
    time.sleep(delay)
    results.append(test_resume_one_page())

    # Test 4: Cover letter
    time.sleep(delay)
    results.append(test_cover_letter_generation())

    # Test 5: Different profiles
    time.sleep(delay)
    results.append(test_different_profiles())

    # Test 6: Resume upload mode
    time.sleep(delay)
    results.append(test_resume_upload_mode())

    # Summary
    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'=' * 55}")
    print(f"  Results: {passed}/{total} passed")
    print(f"{'=' * 55}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
