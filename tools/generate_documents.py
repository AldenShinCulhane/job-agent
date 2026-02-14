"""
Tailored resume & cover letter generator.
Uses the configured LLM for generation and python-docx for DOCX output.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path

import yaml
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, Twips
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from llm_client import chat_completion


def read_base_resume(resume_path: str) -> str:
    """Read the user's base resume from various formats."""
    path = Path(resume_path)
    suffix = path.suffix.lower()

    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8")

    elif suffix == ".docx":
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)

    elif suffix == ".pdf":
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text
        except ImportError:
            raise ImportError("PyPDF2 required for PDF resumes: uv add PyPDF2")

    else:
        # Try reading as text
        return path.read_text(encoding="utf-8")


def slugify(text: str, max_len: int = 40) -> str:
    """Create filesystem-safe slug from text."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:max_len].rstrip("-")


def generate_tailored_resume(job: dict, profile: dict, base_resume: str) -> str:
    """Generate a tailored resume using the configured LLM."""
    analysis = job.get("analysis", {})
    system_prompt = """You are an expert resume writer. Generate a tailored resume that emphasizes
the candidate's relevant skills and experience for this specific role.

Output the resume in this exact structured format:
## CONTACT
[Name]
[Email] | [Phone] | [Location]
[LinkedIn URL]

## EDUCATION
### [Institution] | [Start - End]
[Degree] in [Field] | [Location]

## EXPERIENCE
### [Job Title] | [Start Date] - [End Date]
[Company] | [Location]
- [Achievement bullet tailored to target role]
- [Achievement bullet tailored to target role]

## PROJECTS
### [Project Name] | [Technologies] | [Date Range]
- [Description bullet tailored to target role]

## TECHNICAL SKILLS
### [Category]: [Comma-separated list]
### [Category]: [Comma-separated list]

Return ONLY the resume content. No explanation or commentary."""

    user_msg = f"""Create a tailored resume for this job:

TARGET JOB:
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Required Skills: {', '.join(analysis.get('required_skills', []))}
Preferred Skills: {', '.join(analysis.get('preferred_skills', []))}
Role Summary: {analysis.get('role_summary', 'N/A')}
Key Responsibilities: {json.dumps(analysis.get('key_responsibilities', []))}

CANDIDATE'S BASE RESUME:
{base_resume[:4000]}

CANDIDATE'S PROFILE:
Name: {profile.get('personal', {}).get('name', 'N/A')}
Email: {profile.get('personal', {}).get('email', 'N/A')}
Phone: {profile.get('personal', {}).get('phone', 'N/A')}
Location: {profile.get('personal', {}).get('location', 'N/A')}
LinkedIn: {profile.get('personal', {}).get('linkedin', 'N/A')}
Total Experience: {profile.get('experience', {}).get('total_years', 'N/A')} years

CANDIDATE'S PROJECTS:
{json.dumps([{"name": p.get("name"), "description": p.get("description"), "technologies": p.get("technologies", [])} for p in profile.get("projects", [])], indent=2)}

MATCH INFO:
Score: {job.get('match_score', 'N/A')}%
Matched Skills: {', '.join(job.get('matched_skills', []))}
Missing Skills: {', '.join(job.get('missing_skills', []))}

Tailor the resume to emphasize the matched skills, reorder experience to highlight
relevant achievements, and adjust the summary to align with this specific role.
Do NOT fabricate experience or skills the candidate doesn't have."""

    try:
        return chat_completion(
            system=system_prompt,
            user_message=user_msg,
            max_tokens=3000,
            task="generate",
        )
    except Exception as e:
        print(f"    Resume generation failed: {e}")
        return ""


def generate_cover_letter(job: dict, profile: dict, base_resume: str) -> str:
    """Generate a tailored cover letter using the configured LLM."""
    analysis = job.get("analysis", {})
    system_prompt = """You are an expert career consultant. Write a professional cover letter
that connects the candidate's specific experience to the job requirements.

Format:
## HEADER
[Date]
[Company Name]

## SALUTATION
Dear Hiring Manager,

## BODY
[3-4 paragraphs: opening hook, relevant experience, why this company, closing]

## CLOSING
Sincerely,
[Candidate Name]

Keep it to one page. Be specific — reference actual skills and achievements.
Do NOT use generic filler. Return ONLY the letter content."""

    user_msg = f"""Write a cover letter for:

JOB:
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Role Summary: {analysis.get('role_summary', 'N/A')}
Key Responsibilities: {json.dumps(analysis.get('key_responsibilities', []))}
Culture: {', '.join(analysis.get('culture_signals', []))}

CANDIDATE:
Name: {profile.get('personal', {}).get('name', 'N/A')}
Summary: {profile.get('summary', 'N/A')}
Years of Experience: {profile.get('experience', {}).get('total_years', 'N/A')}
Matched Skills: {', '.join(job.get('matched_skills', []))}

BASE RESUME (for reference):
{base_resume[:3000]}

Connect the candidate's actual experience to this role's specific requirements.
Do NOT fabricate achievements or skills."""

    try:
        return chat_completion(
            system=system_prompt,
            user_message=user_msg,
            max_tokens=2000,
            task="generate",
        )
    except Exception as e:
        print(f"    Cover letter generation failed: {e}")
        return ""


def create_resume_docx(resume_text: str, output_path: str):
    """Convert structured resume text into a formatted DOCX matching the LaTeX resume style.

    Expects markdown-style input with ## SECTION headers, ### sub-entries, and - bullets.
    Produces a one-page resume with Arial font, 0.5" margins, small-caps section headers
    with horizontal rules, and two-column alignment for dates/locations.
    """
    doc = Document()

    # Page setup: letter size, 0.5" margins
    for section in doc.sections:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.5)
        section.right_margin = Inches(0.5)

    # Text width for right-aligned tab stop (97% of 7.5" text area)
    tab_position = Twips(int(7.5 * 0.97 * 1440))

    lines = resume_text.split("\n")
    current_section = None
    # Track whether we just emitted a ### sub-heading pair (for experience entries)
    subheading_line_count = 0

    for line in lines:
        line = line.rstrip()

        # Section heading: ## SECTION
        if line.startswith("## "):
            current_section = line[3:].strip().upper()
            if current_section == "CONTACT":
                continue
            _add_section_header(doc, current_section)
            subheading_line_count = 0
            continue

        # Contact section — centered name + contact line
        if current_section == "CONTACT":
            if line.strip():
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.space_after = Pt(1)
                run = p.add_run(line.strip())
                run.font.name = "Arial"
                # Name line (no special characters) vs contact line
                if not any(c in line for c in ["@", "|", "http", "+"]):
                    run.font.size = Pt(25)
                    run.bold = True
                else:
                    run.font.size = Pt(8)
            continue

        # Sub-heading: ### Title | Company | Dates
        if line.startswith("### "):
            content = line[4:].strip()
            # Split on " | " to separate into left/right columns
            parts = [p.strip() for p in content.split(" | ")]
            if current_section in ("SKILLS", "TECHNICAL SKILLS"):
                # Skills use "**Category**: items" format — render as bold label + text
                _add_skill_line(doc, content)
            elif len(parts) >= 2:
                # Two-column: left content | right content (dates/location)
                left = " | ".join(parts[:-1])
                right = parts[-1]
                _add_two_col_line(doc, left, right, tab_position,
                                  left_bold=True, left_size=Pt(11), right_size=Pt(11))
            else:
                _add_two_col_line(doc, content, "", tab_position,
                                  left_bold=True, left_size=Pt(11), right_size=Pt(11))
            subheading_line_count = 1
            continue

        # Bullet point
        if line.startswith("- "):
            _add_bullet(doc, line[2:].strip())
            subheading_line_count = 0
            continue

        # Non-empty regular text (e.g. second line of experience entry: italic company | location)
        if line.strip():
            if subheading_line_count == 1 and current_section in ("EXPERIENCE", "EDUCATION"):
                # Second line of a sub-heading pair — italic, smaller
                parts = [p.strip() for p in line.strip().split(" | ")]
                if len(parts) >= 2:
                    left = " | ".join(parts[:-1])
                    right = parts[-1]
                else:
                    left = line.strip()
                    right = ""
                _add_two_col_line(doc, left, right, tab_position,
                                  left_italic=True, left_size=Pt(10), right_size=Pt(10))
                subheading_line_count = 2
                continue

            # Generic text line
            p = doc.add_paragraph()
            p.space_before = Pt(0)
            p.space_after = Pt(2)
            run = p.add_run(line.strip())
            run.font.name = "Arial"
            run.font.size = Pt(10)
            subheading_line_count = 0
            continue

        subheading_line_count = 0

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)


def create_cover_letter_docx(letter_text: str, output_path: str):
    """Convert cover letter text into a formatted DOCX file."""
    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    lines = letter_text.split("\n")
    skip_section_header = False

    for line in lines:
        line = line.rstrip()

        # Skip markdown section headers
        if line.startswith("## "):
            skip_section_header = True
            continue

        if not line.strip():
            if not skip_section_header:
                doc.add_paragraph("")
            skip_section_header = False
            continue

        skip_section_header = False

        p = doc.add_paragraph(line.strip())
        for run in p.runs:
            run.font.size = Pt(11)
            run.font.name = "Arial"

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    doc.save(output_path)


def _add_section_header(doc, text: str):
    """Add a small-caps section header with a horizontal rule underneath."""
    p = doc.add_paragraph()
    p.space_before = Pt(12)
    p.space_after = Pt(5)
    run = p.add_run(text.upper())
    run.font.name = "Arial"
    run.font.size = Pt(11)
    run.font.small_caps = True
    # Add bottom border (horizontal rule) via XML
    pPr = p._p.get_or_add_pPr()
    pBdr = pPr.makeelement(qn("w:pBdr"), {})
    bottom = pBdr.makeelement(qn("w:bottom"), {
        qn("w:val"): "single",
        qn("w:sz"): "4",
        qn("w:space"): "1",
        qn("w:color"): "000000",
    })
    pBdr.append(bottom)
    pPr.append(pBdr)


def _add_two_col_line(doc, left: str, right: str, tab_pos,
                      left_bold=False, left_italic=False,
                      left_size=Pt(11), right_size=Pt(11)):
    """Add a line with left text and right-aligned text via a tab stop."""
    p = doc.add_paragraph()
    p.space_before = Pt(2)
    p.space_after = Pt(0)
    # Set right-aligned tab stop
    pPr = p._p.get_or_add_pPr()
    tabs = pPr.makeelement(qn("w:tabs"), {})
    tab = tabs.makeelement(qn("w:tab"), {
        qn("w:val"): "right",
        qn("w:pos"): str(tab_pos),
    })
    tabs.append(tab)
    pPr.append(tabs)
    # Left run
    run_left = p.add_run(left)
    run_left.font.name = "Arial"
    run_left.font.size = left_size
    run_left.bold = left_bold
    run_left.italic = left_italic
    # Tab + right run
    if right:
        p.add_run("\t")
        run_right = p.add_run(right)
        run_right.font.name = "Arial"
        run_right.font.size = right_size


def _add_bullet(doc, text: str):
    """Add a bullet point item."""
    p = doc.add_paragraph(text, style="List Bullet")
    p.space_before = Pt(0)
    p.space_after = Pt(2)
    for run in p.runs:
        run.font.size = Pt(10)
        run.font.name = "Arial"


def _add_skill_line(doc, text: str):
    """Add a skill category line (bold label: comma-separated items)."""
    p = doc.add_paragraph()
    p.space_before = Pt(0)
    p.space_after = Pt(2)
    # Format from paragraph indentation
    fmt = p.paragraph_format
    fmt.left_indent = Inches(0.15)
    if ":" in text:
        label, items = text.split(":", 1)
        run = p.add_run(f"{label.strip()}: ")
        run.font.name = "Arial"
        run.font.size = Pt(10)
        run.bold = True
        run = p.add_run(items.strip())
        run.font.name = "Arial"
        run.font.size = Pt(10)
    else:
        run = p.add_run(text)
        run.font.name = "Arial"
        run.font.size = Pt(10)


def generate_documents(
    jobs_path: str = None,
    profile_path: str = None,
    base_resume_path: str = None,
    output_dir: str = None,
    threshold: float = 35.0,
    max_jobs: int = 20,
) -> int:
    """Generate tailored resumes and cover letters for qualifying jobs.

    Reads scored jobs, filters by threshold, and generates a tailored resume
    and cover letter for each qualifying job using the configured LLM.
    Returns the number of applications generated.
    """
    if jobs_path is None:
        jobs_path = str(PROJECT_ROOT / ".tmp" / "scored_jobs.json")
    if profile_path is None:
        profile_path = str(PROJECT_ROOT / "config" / "user_profile.yaml")
    if output_dir is None:
        output_dir = str(PROJECT_ROOT / "output")

    with open(jobs_path, "r", encoding="utf-8") as f:
        all_jobs = json.load(f)

    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    else:
        profile = {}

    # Filter to qualifying jobs
    qualifying = [j for j in all_jobs if j.get("match_score", 0) >= threshold]
    qualifying = qualifying[:max_jobs]

    if not qualifying:
        print(f"No jobs scoring {threshold}%+. Lower the threshold or adjust your profile.")
        return 0

    print(f"Generating applications for {len(qualifying)} jobs (threshold: {threshold}%)")
    est_api_calls = len(qualifying) * 2
    print(f"Estimated API calls: {est_api_calls} (resume + cover letter each)")

    # Read base resume if provided
    base_resume = ""
    if base_resume_path:
        print(f"Reading base resume: {base_resume_path}")
        base_resume = read_base_resume(base_resume_path)
    else:
        # Build resume text from profile
        base_resume = _profile_to_resume_text(profile)

    generated_count = 0

    for i, job in enumerate(qualifying):
        company_slug = slugify(job.get("company", "unknown"))
        title_slug = slugify(job.get("title", "unknown"))
        job_dir = os.path.join(output_dir, "applications", f"{company_slug}_{title_slug}")

        print(f"\n[{i + 1}/{len(qualifying)}] {job['title']} at {job['company']} ({job['match_score']}%)")

        # Generate resume
        print("  Generating tailored resume...")
        resume_text = generate_tailored_resume(job, profile, base_resume)
        if resume_text:
            resume_path_out = os.path.join(job_dir, "resume.docx")
            create_resume_docx(resume_text, resume_path_out)
            # Also save as markdown for reference
            md_path = os.path.join(job_dir, "resume.md")
            os.makedirs(os.path.dirname(md_path), exist_ok=True)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(resume_text)
            print(f"  -> {resume_path_out}")

        # Generate cover letter
        print("  Generating cover letter...")
        letter_text = generate_cover_letter(job, profile, base_resume)
        if letter_text:
            letter_path_out = os.path.join(job_dir, "cover_letter.docx")
            create_cover_letter_docx(letter_text, letter_path_out)
            md_path = os.path.join(job_dir, "cover_letter.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(letter_text)
            print(f"  -> {letter_path_out}")

        # Save job details
        details_path = os.path.join(job_dir, "job_details.json")
        with open(details_path, "w", encoding="utf-8") as f:
            # Exclude raw description to keep file size reasonable
            details = {k: v for k, v in job.items() if k != "description_text"}
            json.dump(details, f, indent=2, ensure_ascii=False)

        generated_count += 1
        time.sleep(0.5)

    print(f"\nGenerated {generated_count} applications -> {os.path.join(output_dir, 'applications')}")
    return generated_count


def _profile_to_resume_text(profile: dict) -> str:
    """Build a resume text from the profile YAML when no base resume file is provided."""
    parts = []
    personal = profile.get("personal", {})
    parts.append(f"{personal.get('name', 'N/A')}")
    parts.append(f"{personal.get('email', '')} | {personal.get('phone', '')} | {personal.get('location', '')}")
    parts.append("")
    parts.append(f"SUMMARY: {profile.get('summary', '').strip()}")
    parts.append("")

    skills = profile.get("skills", {})
    all_skills = []
    for category_skills in skills.values():
        if isinstance(category_skills, list):
            all_skills.extend(category_skills)
    parts.append(f"SKILLS: {', '.join(all_skills)}")
    parts.append("")

    for pos in profile.get("experience", {}).get("positions", []):
        parts.append(f"{pos.get('title', '')} at {pos.get('company', '')} ({pos.get('start_date', '')} - {pos.get('end_date', '')})")
        for h in pos.get("highlights", []):
            parts.append(f"- {h}")
        parts.append("")

    for proj in profile.get("projects", []):
        tech = ", ".join(proj.get("technologies", []))
        parts.append(f"{proj.get('name', '')} | {tech}")
        if proj.get("description"):
            parts.append(f"- {proj['description']}")
        parts.append("")

    for edu in profile.get("education", []):
        parts.append(f"{edu.get('degree', '')} in {edu.get('field', '')} - {edu.get('institution', '')} ({edu.get('graduation_year', '')})")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Generate tailored resumes and cover letters")
    parser.add_argument("--jobs", default=str(PROJECT_ROOT / ".tmp" / "scored_jobs.json"))
    parser.add_argument("--profile", default=str(PROJECT_ROOT / "config" / "user_profile.yaml"))
    parser.add_argument("--resume", default=None, help="Path to base resume (.txt, .md, .docx, .pdf)")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "output"))
    parser.add_argument("--threshold", type=float, default=35.0)
    parser.add_argument("--max-jobs", type=int, default=20)
    args = parser.parse_args()

    generate_documents(
        jobs_path=args.jobs,
        profile_path=args.profile,
        base_resume_path=args.resume,
        output_dir=args.output_dir,
        threshold=args.threshold,
        max_jobs=args.max_jobs,
    )


if __name__ == "__main__":
    main()
