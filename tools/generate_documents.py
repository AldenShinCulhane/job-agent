"""
Tailored resume & cover letter generator.

Resumes: LaTeX template -> PDF (uses user's exact formatting with LLM-tailored bullets)
Cover letters: LaTeX template -> PDF (contact header + properly spaced body)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from llm_client import chat_completion, get_call_delay


# == LaTeX Resume Template ================================================
# Exact preamble from user's LaTeX resume. All spacing is intentional.

RESUME_PREAMBLE = r"""\documentclass[letterpaper,11pt]{article}
\usepackage{latexsym}
\usepackage[empty]{fullpage}
\usepackage{titlesec}
\usepackage{marvosym}
\usepackage[usenames,dvipsnames]{color}
\usepackage{verbatim}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{fancyhdr}
\usepackage[english]{babel}
\usepackage{tabularx}
\usepackage{fontawesome5}
\usepackage{hyperref}
\usepackage[scaled]{helvet}
\input{glyphtounicode}

\pagestyle{fancy}
\fancyhf{}
\fancyfoot{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\renewcommand{\familydefault}{\sfdefault}

\addtolength{\oddsidemargin}{-0.5in}
\addtolength{\evensidemargin}{-0.5in}
\addtolength{\textwidth}{1in}
\addtolength{\topmargin}{-.5in}
\addtolength{\textheight}{1.0in}

\urlstyle{same}

\raggedbottom
\raggedright
\setlength{\tabcolsep}{0in}

\titleformat{\section}{
  \vspace{-4pt}\scshape\raggedright\normalsize
}{}{0em}{}[\color{black}\titlerule \vspace{-5pt}]
\titlespacing*{\section}{0pt}{12pt}{10pt}

\pdfgentounicode=1

\newcommand{\resumeItem}[1]{
  \item\small{
    {#1 \vspace{-2pt}}
  }
}

\newcommand{\resumeSubheading}[4]{
  \vspace{-2pt}\item
    \begin{tabular*}{0.97\textwidth}[t]{l@{\extracolsep{\fill}}r}
      \textbf{#1} & #2 \\
      \textit{\small#3} & \textit{\small #4} \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeSubSubheading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      \textit{\small#1} & \textit{\small #2} \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeProjectHeading}[2]{
    \item
    \begin{tabular*}{0.97\textwidth}{l@{\extracolsep{\fill}}r}
      \small#1 & #2 \\
    \end{tabular*}\vspace{-7pt}
}

\newcommand{\resumeSubItem}[1]{\resumeItem{#1}\vspace{-4pt}}

\renewcommand\labelitemii{$\vcenter{\hbox{\tiny$\bullet$}}$}

\newcommand{\resumeSubHeadingListStart}{\begin{itemize}[leftmargin=0.15in, label={}]}
\newcommand{\resumeSubHeadingListEnd}{\end{itemize}}
\newcommand{\resumeItemListStart}{\begin{itemize}}
\newcommand{\resumeItemListEnd}{\end{itemize}\vspace{-5pt}}
"""

# Month abbreviations matching the user's LaTeX style
MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "June", 7: "July", 8: "Aug",
    9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec",
}

# Map profile YAML skill keys to resume display names
SKILL_CATEGORY_NAMES = {
    "programming_languages": "Languages",
    "frameworks": "Libraries/Frameworks",
    "tools": "Developer Tools/IDEs",
    "soft_skills": None,  # Skip on technical resume
}


# == Helper Functions =====================================================


def latex_escape(text: str) -> str:
    """Escape LaTeX special characters in plain text."""
    if not text:
        return ""
    text = str(text)
    # Backslash first to avoid double-escaping
    text = text.replace("\\", "\\textbackslash{}")
    for char in "&%$#_{}":
        text = text.replace(char, f"\\{char}")
    text = text.replace("~", "\\textasciitilde{}")
    text = text.replace("^", "\\textasciicircum{}")
    # Unicode dashes to LaTeX
    text = text.replace("\u2013", "--")   # en dash
    text = text.replace("\u2014", "---")  # em dash
    return text


def format_date(date_str: str) -> str:
    """Convert '2024-06' to 'June 2024', 'present' to 'Present'."""
    if not date_str or str(date_str).lower() == "present":
        return "Present"
    date_str = str(date_str)
    if re.match(r"^\d{4}-\d{2}$", date_str):
        year, month = date_str.split("-")
        return f"{MONTH_NAMES.get(int(month), month)} {year}"
    if re.match(r"^\d{4}$", date_str):
        return date_str
    return date_str


def bold_to_textbf(text: str) -> str:
    r"""Convert **bold** markdown to \textbf{bold} LaTeX."""
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)


def _url_display(url: str) -> str:
    """Strip protocol/www from URL for display: https://www.x.com/y -> x.com/y"""
    return (
        url.replace("https://www.", "")
        .replace("http://www.", "")
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )


def slugify(text: str, max_len: int = 40) -> str:
    """Create filesystem-safe slug from text."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text[:max_len].rstrip("-")


def read_base_resume(resume_path: str) -> str:
    """Read the user's base resume from various formats."""
    path = Path(resume_path)
    suffix = path.suffix.lower()

    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8")
    elif suffix == ".docx":
        from docx import Document as DocxDoc
        doc = DocxDoc(str(path))
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
        return path.read_text(encoding="utf-8")


# == Resume: LLM Bullet Tailoring ========================================


def generate_tailored_bullets(job: dict, profile: dict) -> dict | None:
    """Ask LLM to tailor resume bullet points for a specific job.

    Returns dict with 'experience' and 'projects' arrays of tailored bullets,
    or None if the LLM call fails or returns invalid structure (falls back to
    original profile bullets).
    """
    analysis = job.get("analysis", {})
    positions = profile.get("experience", {}).get("positions", [])
    projects = profile.get("projects", [])

    # Build numbered input showing all bullets
    experience_text = ""
    for i, pos in enumerate(positions):
        experience_text += (
            f"\nPosition {i + 1}: {pos.get('title', '')} at {pos.get('company', '')}\n"
        )
        for j, h in enumerate(pos.get("highlights", [])):
            experience_text += f"  {j + 1}. {h}\n"

    projects_text = ""
    for i, proj in enumerate(projects):
        projects_text += f"\nProject {i + 1}: {proj.get('name', '')}\n"
        for j, h in enumerate(proj.get("highlights", [])):
            projects_text += f"  {j + 1}. {h}\n"

    system_prompt = """You tailor resume bullet points for specific job applications.

Rules:
- Keep ALL positions and ALL projects. Never remove entries.
- Keep ALL bullets. Return the EXACT SAME NUMBER of bullets per entry as the input.
- Rewrite each bullet to emphasize skills and experience directly relevant to the target job.
- Write detailed, substantive bullets — do not shorten them. Longer bullets that fill 2-3 printed lines are preferred.
- Use **bold** around key technical terms and tools that match the job requirements.
- Do NOT fabricate experience, achievements, or skills the candidate doesn't have.
- Preserve concrete metrics, numbers, and specific details when rewriting.

Return ONLY valid JSON in this exact structure:
{
  "experience": [
    ["bullet 1 for position 1", "bullet 2 for position 1"],
    ["bullet 1 for position 2", "bullet 2 for position 2", "bullet 3 for position 2"]
  ],
  "projects": [
    ["bullet 1 for project 1", "bullet 2 for project 1"],
    ["bullet 1 for project 2", "bullet 2 for project 2"]
  ]
}

The experience array must have the same number of entries (sub-arrays) as positions given.
The projects array must have the same number of entries (sub-arrays) as projects given.
Each entry must have the same number of bullets as the original."""

    user_msg = f"""TARGET JOB:
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Required Skills: {', '.join(analysis.get('required_skills', []))}
Preferred Skills: {', '.join(analysis.get('preferred_skills', []))}
Role Summary: {analysis.get('role_summary', 'N/A')}
Key Responsibilities: {json.dumps(analysis.get('key_responsibilities', []))}

MATCH INFO:
Matched Skills: {', '.join(job.get('matched_skills', []))}
Missing Skills: {', '.join(job.get('missing_skills', []))}

EXPERIENCE BULLETS TO TAILOR:
{experience_text}
PROJECT BULLETS TO TAILOR:
{projects_text}
Tailor these bullets for the target job. Return JSON only."""

    try:
        response = chat_completion(
            system=system_prompt,
            user_message=user_msg,
            max_tokens=8192,
            task="generate",
        )

        # Strip markdown code fences if present
        response = response.strip()
        if response.startswith("```"):
            response = re.sub(r"^```(?:json)?\s*\n?", "", response)
            response = re.sub(r"\n?```\s*$", "", response)

        result = json.loads(response)

        # Validate structure — tolerant: accept ±1 count, fall back per-position if too far off
        exp_bullets = result.get("experience", [])
        proj_bullets = result.get("projects", [])

        # Fix experience bullets (per-position fallback)
        validated_exp = []
        for i, pos in enumerate(positions):
            expected = len(pos.get("highlights", []))
            returned = len(exp_bullets[i]) if i < len(exp_bullets) else 0
            if returned == expected:
                validated_exp.append(exp_bullets[i])
            elif returned == expected - 1:
                # 1 short — tailored bullets are still better than originals
                validated_exp.append(exp_bullets[i])
            elif returned > expected:
                # Extra bullets — trim to expected count
                validated_exp.append(exp_bullets[i][:expected])
            else:
                # Too far off — fall back to originals
                if i < len(exp_bullets):
                    print(f"    Position {i + 1}: LLM returned {returned} bullets (expected {expected}), using originals")
                validated_exp.append(pos.get("highlights", []))

        # Fix project bullets (per-project fallback)
        validated_proj = []
        for i, proj in enumerate(projects):
            expected = len(proj.get("highlights", []))
            returned = len(proj_bullets[i]) if i < len(proj_bullets) else 0
            if returned == expected:
                validated_proj.append(proj_bullets[i])
            elif returned == expected - 1:
                validated_proj.append(proj_bullets[i])
            elif returned > expected:
                validated_proj.append(proj_bullets[i][:expected])
            else:
                if i < len(proj_bullets):
                    print(f"    Project {i + 1}: LLM returned {returned} bullets (expected {expected}), using originals")
                validated_proj.append(proj.get("highlights", []))

        return {"experience": validated_exp, "projects": validated_proj}

    except json.JSONDecodeError as e:
        print(f"    Bullet tailoring failed (invalid JSON): {e}")
        print(f"    Response preview: {response[:200]}")
        print(f"    Using original bullets.")
        return None
    except Exception as e:
        print(f"    Bullet tailoring failed: {e}. Using original bullets.")
        return None


# == Resume: LaTeX Assembly ===============================================


def _process_bullet(text: str) -> str:
    r"""Escape a bullet point for LaTeX and convert **bold** to \textbf{}."""
    escaped = latex_escape(text)
    return bold_to_textbf(escaped)


def build_resume_tex(profile: dict, tailored_bullets: dict | None) -> str:
    """Assemble a complete .tex document from profile data and tailored bullets."""
    personal = profile.get("personal", {})
    positions = profile.get("experience", {}).get("positions", [])
    projects = profile.get("projects", [])
    education_list = profile.get("education", [])
    skills = profile.get("skills", {})
    default_location = personal.get("location", "")

    exp_bullets = tailored_bullets.get("experience", []) if tailored_bullets else []
    proj_bullets = tailored_bullets.get("projects", []) if tailored_bullets else []

    tex = RESUME_PREAMBLE + "\n\n\\begin{document}\n\n"

    # -- Heading --
    name = latex_escape(personal.get("name", ""))
    phone = latex_escape(personal.get("phone", ""))
    email = personal.get("email", "")
    linkedin = personal.get("linkedin", "")
    github = personal.get("github", "")

    tex += "\\begin{center}\n"
    tex += f"    \\textbf{{\\Huge {name}}} \\\\[1pt]\n"
    tex += "    {\\footnotesize\n"

    contact_parts = []
    if phone:
        contact_parts.append(f"\\faIcon{{phone}}~{phone}")
    if email:
        contact_parts.append(
            f"\\faIcon{{envelope}}~\\href{{mailto:{email}}}{{{latex_escape(email)}}}"
        )
    if linkedin:
        display = latex_escape(_url_display(linkedin))
        contact_parts.append(f"\\faIcon{{linkedin}}~\\href{{{linkedin}}}{{{display}}}")
    if github:
        display = latex_escape(_url_display(github))
        contact_parts.append(f"\\faIcon{{github}}~\\href{{{github}}}{{{display}}}")

    tex += "        " + " \\,\\textbar\\,\n        ".join(contact_parts) + "\n"
    tex += "    }\n"
    tex += "\\end{center}\n"

    # -- Experience --
    if positions:
        tex += "%-----------EXPERIENCE-----------\n"
        tex += "\\section{Experience}\n"
        tex += "  \\resumeSubHeadingListStart\n"

        for i, pos in enumerate(positions):
            title = latex_escape(pos.get("title", ""))
            company = latex_escape(pos.get("company", ""))
            location = latex_escape(pos.get("location", default_location))
            start = format_date(pos.get("start_date", ""))
            end = format_date(pos.get("end_date", ""))
            date_range = f"{start} -- {end}"

            tex += "    \\resumeSubheading\n"
            tex += f"      {{{title}}}{{{date_range}}}\n"
            tex += f"      {{{company}}}{{{location}}}\n"
            tex += "      \\resumeItemListStart\n"

            bullets = (
                exp_bullets[i]
                if i < len(exp_bullets)
                else pos.get("highlights", [])
            )
            for bullet in bullets:
                tex += f"        \\resumeItem{{{_process_bullet(bullet)}}}\n"

            tex += "      \\resumeItemListEnd\n\n"

        tex += "  \\resumeSubHeadingListEnd\n\n"

    # -- Projects --
    if projects:
        tex += "%-----------PROJECTS-----------\n"
        tex += "\\section{Projects}\n"
        tex += "    \\resumeSubHeadingListStart\n"

        for i, proj in enumerate(projects):
            name_esc = latex_escape(proj.get("name", ""))
            techs = ", ".join(proj.get("technologies", []))
            techs_esc = latex_escape(techs)
            start = format_date(proj.get("start_date", ""))
            end = format_date(proj.get("end_date", ""))
            date_range = f"{start} -- {end}"

            tex += "      \\resumeProjectHeading\n"
            tex += f"          {{\\textbf{{{name_esc}}} $|$ \\emph{{{techs_esc}}}}}{{{date_range}}}\n"
            tex += "          \\resumeItemListStart\n"

            bullets = (
                proj_bullets[i]
                if i < len(proj_bullets)
                else proj.get("highlights", [])
            )
            for bullet in bullets:
                tex += f"            \\resumeItem{{{_process_bullet(bullet)}}}\n"

            tex += "          \\resumeItemListEnd\n"

        tex += "    \\resumeSubHeadingListEnd\n\n"

    # -- Education --
    if education_list:
        tex += "%-----------EDUCATION-----------\n"
        tex += "\\section{Education}\n"
        tex += "  \\resumeSubHeadingListStart\n"

        for edu in education_list:
            institution = latex_escape(edu.get("institution", ""))
            location = latex_escape(edu.get("location", default_location))

            # Build degree line
            degree = edu.get("degree", "")
            field = edu.get("field", "")
            if " from " in degree:
                degree = degree.split(" from ")[0]
            if field and field.lower() not in degree.lower():
                degree = f"{degree} in {field}"
            degree_esc = latex_escape(degree)

            honors = edu.get("honors", "")
            if honors:
                degree_esc += f", {latex_escape(honors)}"

            # Date range
            start_year = edu.get("start_year")
            grad_year = edu.get("graduation_year")
            if start_year and grad_year:
                date_range = f"Sept {start_year} -- Aug {grad_year}"
            elif grad_year:
                date_range = str(grad_year)
            else:
                date_range = ""

            tex += "    \\resumeSubheading\n"
            tex += f"      {{{institution}}}{{{location}}}\n"
            tex += f"      {{{degree_esc}}}{{{date_range}}}\n"

        tex += "  \\resumeSubHeadingListEnd\n\n"

    # -- Technical Skills --
    if skills:
        tex += "%-----------PROGRAMMING SKILLS-----------\n"
        tex += "\\section{Technical Skills}\n"
        tex += " \\begin{itemize}[leftmargin=0.15in, label={}]\n"
        tex += "    \\small{\\item{\n"

        skill_lines = []
        for category, items in skills.items():
            if not isinstance(items, list) or not items:
                continue
            if category in SKILL_CATEGORY_NAMES:
                display_name = SKILL_CATEGORY_NAMES[category]
                if display_name is None:
                    continue
            else:
                display_name = category.replace("_", " ").title()
            items_str = ", ".join(latex_escape(s) for s in items)
            skill_lines.append(f"     \\textbf{{{display_name}}}{{: {items_str}}}")

        tex += " \\\\\n".join(skill_lines) + "\n"
        tex += "     }}\n"
        tex += " \\end{itemize}\n\n"

    tex += "\\end{document}\n"
    return tex


def _find_pdflatex() -> str | None:
    """Find pdflatex executable, checking PATH then common install locations."""
    found = shutil.which("pdflatex")
    if found:
        return found

    # Windows: MiKTeX doesn't always add itself to PATH
    if sys.platform == "win32":
        candidates = [
            Path.home() / "AppData" / "Local" / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "pdflatex.exe",
            Path("C:/Program Files/MiKTeX/miktex/bin/x64/pdflatex.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    return None


def compile_tex_to_pdf(tex_path: str) -> bool:
    """Compile .tex to .pdf using pdflatex. Returns True if PDF was created."""
    output_dir = os.path.dirname(tex_path)
    pdflatex = _find_pdflatex()

    if not pdflatex:
        print("    pdflatex not found. Install a LaTeX distribution to compile PDFs:")
        print("      Windows: https://miktex.org/download")
        print("      macOS: brew install --cask mactex-no-gui")
        print("      Linux: sudo apt install texlive-full")
        print("    The .tex file has been saved. Compile manually: pdflatex resume.tex")
        return False

    # MiKTeX: use --enable-installer to auto-download missing packages
    is_miktex = "miktex" in pdflatex.lower()
    cmd = [pdflatex, "-interaction=nonstopmode", "-output-directory", output_dir]
    if is_miktex:
        cmd.insert(1, "--enable-installer")
    cmd.append(tex_path)

    try:
        # Run pdflatex twice (cross-references)
        # Longer timeout for first run (MiKTeX may download packages)
        for _ in range(2):
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )

        pdf_path = tex_path.replace(".tex", ".pdf")
        success = os.path.exists(pdf_path)

        # Clean up auxiliary files
        base = tex_path.replace(".tex", "")
        for ext in [".aux", ".log", ".out"]:
            aux_path = base + ext
            if os.path.exists(aux_path):
                os.remove(aux_path)

        if not success:
            print("    pdflatex failed. Check the .tex file for errors.")

        return success

    except subprocess.TimeoutExpired:
        print("    pdflatex timed out after 180s (MiKTeX may be downloading packages).")
        print("    Try running manually: pdflatex resume.tex")
        return False


def _get_pdf_page_count(pdf_path: str) -> int:
    """Return the number of pages in a PDF file."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(pdf_path)
        return len(reader.pages)
    except Exception:
        return 1  # Assume 1 page if we can't read it


def enforce_one_page(tex_path: str, profile: dict, tailored_bullets: dict | None) -> bool:
    """Check if compiled PDF exceeds 1 page; if so, trim content and recompile.

    Trimming strategy (applied in order until PDF fits on 1 page):
      1. Remove the shortest bullet from positions/projects with >2 bullets (repeat)
      2. Remove the last project
      3. Tighten LaTeX spacing

    Returns True if the final PDF is 1 page (or pdflatex is unavailable).
    """
    pdf_path = tex_path.replace(".tex", ".pdf")
    if not os.path.exists(pdf_path):
        return False

    pages = _get_pdf_page_count(pdf_path)
    if pages <= 1:
        return True

    print(f"    Resume is {pages} pages — trimming to fit 1 page...")

    positions = profile.get("experience", {}).get("positions", [])
    projects = profile.get("projects", [])
    exp_bullets = tailored_bullets.get("experience", []) if tailored_bullets else []
    proj_bullets = tailored_bullets.get("projects", []) if tailored_bullets else []

    # Build mutable bullet lists (copy originals if tailored not available)
    mut_exp = []
    for i, pos in enumerate(positions):
        if i < len(exp_bullets):
            mut_exp.append(list(exp_bullets[i]))
        else:
            mut_exp.append(list(pos.get("highlights", [])))

    mut_proj = []
    for i, proj in enumerate(projects):
        if i < len(proj_bullets):
            mut_proj.append(list(proj_bullets[i]))
        else:
            mut_proj.append(list(proj.get("highlights", [])))

    trimmed_profile = dict(profile)

    # Pass 1: Remove least-relevant bullets (shortest bullet from positions with >2 bullets)
    changed = True
    while changed:
        changed = False
        for bullet_list in mut_exp + mut_proj:
            if len(bullet_list) > 2:
                # Remove the shortest bullet (least content)
                shortest_idx = min(range(len(bullet_list)), key=lambda k: len(bullet_list[k]))
                bullet_list.pop(shortest_idx)
                changed = True
                break  # Recompile after each removal to check page count

        trimmed_tailored = {"experience": mut_exp, "projects": mut_proj}
        tex_content = build_resume_tex(trimmed_profile, trimmed_tailored)
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex_content)
        if compile_tex_to_pdf(tex_path) and _get_pdf_page_count(pdf_path) <= 1:
            print("    Removed bullets to fit 1 page.")
            return True

    # Pass 2: Remove last project
    if len(mut_proj) > 1:
        mut_proj.pop()
        # Also remove from profile copy so build_resume_tex skips it
        trimmed_projects = list(projects[:-1])
        trimmed_profile = dict(profile)
        trimmed_profile["projects"] = trimmed_projects
        trimmed_tailored = {"experience": mut_exp, "projects": mut_proj}
        tex_content = build_resume_tex(trimmed_profile, trimmed_tailored)
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(tex_content)
        if compile_tex_to_pdf(tex_path) and _get_pdf_page_count(pdf_path) <= 1:
            print("    Removed last project to fit 1 page.")
            return True

    # Pass 3: Tighten spacing
    tex_content = tex_content.replace(
        r"\addtolength{\textheight}{1.0in}",
        r"\addtolength{\textheight}{1.5in}",
    ).replace(
        r"\addtolength{\topmargin}{-.5in}",
        r"\addtolength{\topmargin}{-.7in}",
    ).replace(
        r"\titlespacing*{\section}{0pt}{12pt}{10pt}",
        r"\titlespacing*{\section}{0pt}{6pt}{4pt}",
    )
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex_content)
    if compile_tex_to_pdf(tex_path) and _get_pdf_page_count(pdf_path) <= 1:
        print("    Tightened spacing to fit 1 page.")
        return True

    print("    Warning: Could not fit resume to 1 page after trimming.")
    return False


# == Cover Letter =========================================================

# At 12pt Arial with 1" margins on letter paper, ~3,000 chars fills one page.
# Contact header (name + location + email + github + linkedin) takes ~5 lines of overhead.
COVER_LETTER_MAX_CHARS = 2500


def _trim_cover_letter(text: str) -> str:
    """Trim cover letter text to fit on 1 page (~2,500 chars for body).

    Strips any date line the LLM may have added, then trims the longest
    body paragraph if still over budget (preserving opening/closing).
    """
    # Strip date line if present (e.g. "February 14, 2026" or "02/14/2026")
    date_pattern = re.compile(
        r"^(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}$"
        r"|^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$",
        re.IGNORECASE,
    )
    cleaned_lines = text.split("\n")
    # Check first few non-empty lines for a date
    for idx in range(min(3, len(cleaned_lines))):
        if cleaned_lines[idx].strip() and date_pattern.match(cleaned_lines[idx].strip()):
            cleaned_lines.pop(idx)
            # Remove trailing blank line after date
            if idx < len(cleaned_lines) and not cleaned_lines[idx].strip():
                cleaned_lines.pop(idx)
            break
    text = "\n".join(cleaned_lines).strip()

    if len(text) <= COVER_LETTER_MAX_CHARS:
        return text

    lines = text.split("\n")
    # Find paragraph boundaries (blank-line separated)
    paragraphs = []
    current = []
    for line in lines:
        if not line.strip():
            if current:
                paragraphs.append("\n".join(current))
                current = []
            paragraphs.append("")  # blank line
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))

    # Find the longest body paragraph (skip date, greeting, closing/signature)
    body_indices = []
    for i, p in enumerate(paragraphs):
        stripped = p.strip()
        if not stripped:
            continue
        # Skip date lines, greeting, sincerely, name (short lines)
        if len(stripped) < 40 or stripped.startswith("Dear ") or stripped.startswith("Sincerely"):
            continue
        body_indices.append(i)

    if not body_indices:
        return text

    # Trim the longest body paragraph by cutting sentences from the end
    longest_idx = max(body_indices, key=lambda i: len(paragraphs[i]))
    para = paragraphs[longest_idx]
    sentences = re.split(r'(?<=[.!?])\s+', para)
    while len("\n".join(paragraphs)) > COVER_LETTER_MAX_CHARS and len(sentences) > 2:
        sentences.pop()
        paragraphs[longest_idx] = " ".join(sentences)

    result = "\n".join(paragraphs)
    if len(result) > COVER_LETTER_MAX_CHARS:
        print("    Note: cover letter may exceed 1 page")
    return result


def generate_cover_letter(job: dict, profile: dict, base_resume: str) -> str:
    """Generate a tailored cover letter using the configured LLM."""
    analysis = job.get("analysis", {})
    personal = profile.get("personal", {})

    system_prompt = """You are an expert career consultant. Write a professional cover letter
that connects the candidate's specific experience to the job requirements.

Write the letter as plain text with NO formatting markers (no ##, no **, no markdown).
Include in this exact order:
1. "Dear Hiring Manager,"
2. A blank line
3. 4 substantive, detailed paragraphs:
   - Opening: express enthusiasm and summarize your fit for this specific role
   - Experience: describe relevant experience with concrete details, metrics, and achievements
   - Company fit: explain why this specific company and role excites you, referencing their mission or culture
   - Closing: restate your value, express eagerness for next steps
4. A blank line
5. "Sincerely,"
6. The candidate's full name

Do NOT include a date line.
The letter MUST fit on exactly 1 page when printed at 12pt font with 1-inch margins.
Keep the total body text under 350 words (about 2,500 characters).
Each paragraph should be 3-5 sentences with specific details — not generic filler.
Be specific: reference actual skills, tools, and achievements from the candidate's resume.
Do NOT fabricate achievements or skills the candidate does not have.
Return ONLY the letter text."""

    user_msg = f"""Write a cover letter for:

JOB:
Title: {job.get('title', 'N/A')}
Company: {job.get('company', 'N/A')}
Role Summary: {analysis.get('role_summary', 'N/A')}
Key Responsibilities: {json.dumps(analysis.get('key_responsibilities', []))}
Culture: {', '.join(analysis.get('culture_signals', []))}

CANDIDATE:
Name: {personal.get('name', 'N/A')}
Summary: {profile.get('summary', 'N/A')}
Years of Experience: {profile.get('experience', {}).get('total_years', 'N/A')}
Matched Skills: {', '.join(job.get('matched_skills', []))}

BASE RESUME (for reference):
{base_resume[:3000]}

Connect the candidate's actual experience to this role's specific requirements."""

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


def create_cover_letter_pdf(letter_text: str, profile: dict, output_dir: str):
    """Create a PDF cover letter via LaTeX with contact info header and body text.

    Writes cover_letter.tex and compiles to cover_letter.pdf.
    Returns True if PDF was created, False if only .tex (no pdflatex).
    """
    personal = profile.get("personal", {})

    # -- Build LaTeX source --
    tex = r"""\documentclass[11pt,letterpaper]{article}
\usepackage[margin=1in]{geometry}
\usepackage{parskip}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\begin{document}
"""

    # Contact info block
    contact_lines = []
    for field in ["name", "location", "email", "github", "linkedin"]:
        value = personal.get(field, "")
        if value:
            contact_lines.append(latex_escape(value))
    if contact_lines:
        tex += "\n".join(contact_lines) + "\n\n"
        tex += "\\vspace{0.5em}\n\n"

    # Body text
    lines = letter_text.split("\n")
    for line in lines:
        stripped = line.strip()

        # Skip markdown headers the LLM might include
        if stripped.startswith("## ") or stripped.startswith("# "):
            continue

        if not stripped:
            tex += "\n"
            continue

        tex += latex_escape(stripped) + "\n"

    tex += "\n\\end{document}\n"

    # Write .tex and compile
    os.makedirs(output_dir, exist_ok=True)
    tex_path = os.path.join(output_dir, "cover_letter.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex)

    return compile_tex_to_pdf(tex_path)


# == File Management ======================================================


def _backup_existing(job_dir: str):
    """Rename existing output files with _old suffix before writing new ones."""
    backup_names = [
        "resume.pdf", "resume.tex", "resume.docx", "resume.md",
        "cover_letter.pdf", "cover_letter.tex", "cover_letter.md",
    ]
    for name in backup_names:
        path = os.path.join(job_dir, name)
        if os.path.exists(path):
            old_path = os.path.join(job_dir, name.replace(".", "_old.", 1))
            if os.path.exists(old_path):
                os.remove(old_path)
            os.rename(path, old_path)


# == Main Entry Point =====================================================


def generate_documents(
    jobs_path: str = None,
    profile_path: str = None,
    base_resume_path: str = None,
    output_dir: str = None,
    threshold: float = 35.0,
    max_jobs: int = 20,
) -> int:
    """Generate tailored resumes and cover letters for qualifying jobs.

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

    if profile_path and os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
    else:
        profile = {}

    has_profile = bool(profile.get("personal"))

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
    elif has_profile:
        base_resume = _profile_to_resume_text(profile)

    generated_count = 0

    for i, job in enumerate(qualifying):
        company_slug = slugify(job.get("company", "unknown"))
        title_slug = slugify(job.get("title", "unknown"))
        job_dir = os.path.join(output_dir, "applications", f"{company_slug}_{title_slug}")

        print(
            f"\n[{i + 1}/{len(qualifying)}] "
            f"{job['title']} at {job['company']} ({job['match_score']}%)"
        )

        # Backup existing files before overwriting
        _backup_existing(job_dir)
        os.makedirs(job_dir, exist_ok=True)

        # -- Resume (LaTeX -> PDF) --
        if has_profile:
            print("  Generating tailored resume (LaTeX)...")
            tailored = generate_tailored_bullets(job, profile)
            tex_content = build_resume_tex(profile, tailored)

            tex_path = os.path.join(job_dir, "resume.tex")
            with open(tex_path, "w", encoding="utf-8") as f:
                f.write(tex_content)

            if compile_tex_to_pdf(tex_path):
                enforce_one_page(tex_path, profile, tailored)
                print(f"  -> {os.path.join(job_dir, 'resume.pdf')}")
            else:
                print(f"  -> {tex_path} (compile manually with pdflatex)")
        else:
            print("  Skipping resume (no profile). Set up with: uv run python tools/setup.py")

        # Wait between LLM calls to respect provider rate limits
        if has_profile:
            time.sleep(get_call_delay())

        # -- Cover Letter (PDF) --
        print("  Generating cover letter...")
        letter_text = generate_cover_letter(job, profile, base_resume)
        if letter_text:
            letter_text = _trim_cover_letter(letter_text)
            pdf_ok = create_cover_letter_pdf(letter_text, profile, job_dir)

            md_path = os.path.join(job_dir, "cover_letter.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(letter_text)
            if pdf_ok:
                print(f"  -> {os.path.join(job_dir, 'cover_letter.pdf')}")
            else:
                print(f"  -> {os.path.join(job_dir, 'cover_letter.tex')} (compile manually with pdflatex)")

        # -- Job Details --
        details_path = os.path.join(job_dir, "job_details.json")
        with open(details_path, "w", encoding="utf-8") as f:
            details = {k: v for k, v in job.items() if k != "description_text"}
            json.dump(details, f, indent=2, ensure_ascii=False)

        generated_count += 1

        # Wait before next job's LLM calls
        if i < len(qualifying) - 1:
            time.sleep(get_call_delay())

    print(f"\nGenerated {generated_count} applications -> {os.path.join(output_dir, 'applications')}")
    return generated_count


def _profile_to_resume_text(profile: dict) -> str:
    """Build a resume text from the profile YAML (used as LLM context for cover letters)."""
    parts = []
    personal = profile.get("personal", {})
    parts.append(f"{personal.get('name', 'N/A')}")
    parts.append(
        f"{personal.get('email', '')} | {personal.get('phone', '')} | "
        f"{personal.get('location', '')}"
    )
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
        parts.append(
            f"{pos.get('title', '')} at {pos.get('company', '')} "
            f"({pos.get('start_date', '')} - {pos.get('end_date', '')})"
        )
        for h in pos.get("highlights", []):
            parts.append(f"- {h}")
        parts.append("")

    for proj in profile.get("projects", []):
        tech = ", ".join(proj.get("technologies", []))
        dates = f" ({proj.get('start_date', '')} - {proj.get('end_date', '')})"
        parts.append(f"{proj.get('name', '')} | {tech}{dates}")
        for h in proj.get("highlights", []):
            parts.append(f"- {h}")
        parts.append("")

    for edu in profile.get("education", []):
        parts.append(
            f"{edu.get('degree', '')} in {edu.get('field', '')} - "
            f"{edu.get('institution', '')} ({edu.get('graduation_year', '')})"
        )

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Generate tailored resumes and cover letters")
    parser.add_argument("--jobs", default=str(PROJECT_ROOT / ".tmp" / "scored_jobs.json"))
    parser.add_argument("--profile", default=str(PROJECT_ROOT / "config" / "user_profile.yaml"))
    parser.add_argument(
        "--resume", default=None, help="Path to base resume (.txt, .md, .docx, .pdf)"
    )
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
