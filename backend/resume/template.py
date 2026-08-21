"""Structured profile -> LaTeX, in the proven resume_subheading2 style.

The LLM never writes LaTeX (PLAN.md §4.5): every string that reaches this
module is escaped here, and the whole document is assembled from these
templates. Layout knobs that page_limit.py will tune later are collected
in Layout with the proven template's values as defaults.
"""

import re
from dataclasses import dataclass

# Recorded in package provenance; bump when this deterministic
# transformation changes the rendered output.
TEMPLATE_VERSION = 1

# Typographic normalization applied before escaping. pdfLaTeX (2018+) and
# Tectonic both take UTF-8, but consistent ASCII punctuation renders
# identically on both engines.
_TYPOGRAPHY = {
    "\u2014": "---",
    "\u2013": "--",
    "\u2018": "`",
    "\u2019": "'",
    "\u201c": "``",
    "\u201d": "''",
    "\u2026": "...",
    "\u2022": "",
    "\u00a0": "~",
}

_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def latex_escape(text: str) -> str:
    for src, dst in _TYPOGRAPHY.items():
        text = text.replace(src, dst)
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def rich(text: str) -> str:
    """Escape, then honor **bold** markers (the only markup we accept)."""
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", latex_escape(text))


@dataclass(frozen=True)
class Layout:
    """Tunable layout parameters. Defaults are the proven template's
    values; page_limit.py may tighten them within readability floors."""

    font_pt: int = 11
    side_margin_add_in: float = 0.5
    top_margin_add_in: float = 0.5
    text_height_add_in: float = 1.25
    section_before_pt: int = 12
    section_after_pt: int = 10
    item_sep_pt: int = 1
    item_gap_pt: int = -2
    subheading_gap_pt: int = -7
    itemlist_end_gap_pt: int = -6


DEFAULT_LAYOUT = Layout()


def _preamble(layout: Layout) -> str:
    return rf"""\documentclass[letterpaper,{layout.font_pt}pt]{{article}}
\usepackage{{latexsym}}
\usepackage[empty]{{fullpage}}
\usepackage{{titlesec}}
\usepackage{{marvosym}}
\usepackage[usenames,dvipsnames]{{color}}
\usepackage{{verbatim}}
\usepackage{{enumitem}}
\usepackage[hidelinks]{{hyperref}}
\usepackage{{fancyhdr}}
\usepackage[english]{{babel}}
\usepackage{{tabularx}}
\usepackage{{fontawesome5}}
% The sans face, per engine. helvet's Type 1 substitution only applies
% under pdfTeX; under XeTeX (what tectonic runs) it silently leaves the
% document in Latin Modern, so load the same URW Helvetica clone as an
% OpenType face at helvet's own 0.95 scale. Same look either way.
\usepackage{{iftex}}
\ifPDFTeX
  \usepackage[scaled]{{helvet}}
\else
  \usepackage{{fontspec}}
  \setsansfont{{texgyreheros}}[Extension=.otf, Scale=0.95,
    UprightFont=*-regular, BoldFont=*-bold, ItalicFont=*-italic,
    BoldItalicFont=*-bolditalic]
\fi

\pagestyle{{fancy}}
\fancyhf{{}}
\fancyfoot{{}}
\renewcommand{{\headrulewidth}}{{0pt}}
\renewcommand{{\footrulewidth}}{{0pt}}
\renewcommand{{\familydefault}}{{\sfdefault}}

\addtolength{{\oddsidemargin}}{{-{layout.side_margin_add_in}in}}
\addtolength{{\evensidemargin}}{{-{layout.side_margin_add_in}in}}
\addtolength{{\textwidth}}{{{2 * layout.side_margin_add_in}in}}
\addtolength{{\topmargin}}{{-{layout.top_margin_add_in}in}}
\addtolength{{\textheight}}{{{layout.text_height_add_in}in}}

\urlstyle{{same}}
\raggedbottom
\raggedright
\setlength{{\tabcolsep}}{{0in}}

\titleformat{{\section}}{{
  \vspace{{-4pt}}\scshape\raggedright\normalsize
}}{{}}{{0em}}{{}}[\color{{black}}\titlerule \vspace{{-5pt}}]
\titlespacing*{{\section}}{{0pt}}{{{layout.section_before_pt}pt}}{{{layout.section_after_pt}pt}}

% Machine-readable / ATS-parsable PDF. Both of these are pdfTeX
% primitives: under XeTeX they are undefined, and touching them aborts the
% whole compile ("! Undefined control sequence. \pdfglyphtounicode").
% XeTeX gets the same property from the OpenType face above, which carries
% its own Unicode mapping.
\ifPDFTeX
  \input{{glyphtounicode}}
  \pdfgentounicode=1
\fi

\newcommand{{\resumeItem}}[1]{{
  \item\small{{
    {{#1\vspace{{{layout.item_gap_pt}pt}}}}
  }}
}}

\newcommand{{\resumeSubheading}}[4]{{
  \vspace{{-2pt}}\item
    \begin{{tabular*}}{{0.97\textwidth}}[t]{{l@{{\extracolsep{{\fill}}}}r}}
      \textbf{{#1}} & #2 \\
      \textit{{\small#3}} & \textit{{\small #4}} \\
    \end{{tabular*}}\vspace{{{layout.subheading_gap_pt}pt}}
}}

\newcommand{{\resumeProjectHeading}}[2]{{
    \item
    \begin{{tabular*}}{{0.97\textwidth}}{{l@{{\extracolsep{{\fill}}}}r}}
      \small#1 & #2 \\
    \end{{tabular*}}\vspace{{{layout.subheading_gap_pt}pt}}
}}

\renewcommand\labelitemii{{$\vcenter{{\hbox{{\tiny$\bullet$}}}}$}}

\newcommand{{\resumeSubHeadingListStart}}{{\begin{{itemize}}[leftmargin=0.15in, label={{}}]}}
\newcommand{{\resumeSubHeadingListEnd}}{{\end{{itemize}}}}
\newcommand{{\resumeItemListStart}}{{\begin{{itemize}}[itemsep={layout.item_sep_pt}pt, parsep=0pt]}}
\newcommand{{\resumeItemListEnd}}{{\end{{itemize}}\vspace{{{layout.itemlist_end_gap_pt}pt}}}}
"""


def _link(url: str | None, icon: str) -> str | None:
    if not url:
        return None
    display = re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")
    href = url.strip() if url.strip().startswith("http") else f"https://{display}"
    return rf"\faIcon{{{icon}}}~\href{{{latex_escape(href)}}}{{{latex_escape(display)}}}"


def _heading(profile: dict) -> str:
    contact = profile.get("contact") or {}
    parts: list[str] = []
    if contact.get("phone"):
        parts.append(rf"\faIcon{{phone}}~{latex_escape(contact['phone'])}")
    if contact.get("email"):
        email = latex_escape(contact["email"])
        parts.append(rf"\faIcon{{envelope}}~\href{{mailto:{email}}}{{{email}}}")
    for key, icon in (("linkedin", "linkedin"), ("github", "github"), ("website", "globe")):
        link = _link(contact.get(key), icon)
        if link:
            parts.append(link)
    if contact.get("location"):
        parts.append(rf"\faIcon{{map-marker-alt}}~{latex_escape(contact['location'])}")

    lines = [rf"    \textbf{{\Huge {latex_escape(profile['name'])}}} \\[1pt]"]
    if parts:
        joined = " \\,\\textbar\\,\n        ".join(parts)
        lines.append("    {\\footnotesize\n        " + joined + "\n    } \\\\[3pt]")
    if profile.get("headline"):
        headline = " \\,\\textbar\\, ".join(
            latex_escape(p.strip()) for p in re.split(r"\s*\|\s*", profile["headline"])
        )
        lines.append(rf"    {{\small \textbf{{{headline}}}}}")
    return "\\begin{center}\n" + "\n".join(lines) + "\n\\end{center}"


def _bullets(bullets: list[str]) -> str:
    if not bullets:
        return ""
    items = "\n".join(rf"        \resumeItem{{{rich(b)}}}" for b in bullets)
    return "      \\resumeItemListStart\n" + items + "\n      \\resumeItemListEnd"


def _experience_section(experiences: list[dict]) -> str:
    entries = []
    for exp in experiences:
        dates = f"{latex_escape(exp.get('start') or '')} -- {latex_escape(exp.get('end') or '')}"
        entry = (
            "    \\resumeSubheading\n"
            f"      {{{latex_escape(exp['title'])}}}{{{dates}}}\n"
            f"      {{{latex_escape(exp['company'])}}}{{{latex_escape(exp.get('location') or '')}}}\n"
            f"{_bullets(exp.get('bullets', []))}"
        )
        entries.append(entry)
    return (
        "\\section{Experience}\n  \\resumeSubHeadingListStart\n"
        + "\n\n".join(entries)
        + "\n  \\resumeSubHeadingListEnd"
    )


def _projects_section(projects: list[dict]) -> str:
    entries = []
    for proj in projects:
        name = latex_escape(proj["name"])
        if proj.get("link"):
            href = proj["link"] if proj["link"].startswith("http") else f"https://{proj['link']}"
            name = rf"\href{{{latex_escape(href)}}}{{{name}}}"
        tech = ", ".join(latex_escape(t) for t in proj.get("technologies", []))
        head = rf"\textbf{{{name}}}" + (rf" $|$ \emph{{{tech}}}" if tech else "")
        date = latex_escape(proj.get("date") or "")
        entries.append(
            "      \\resumeProjectHeading\n"
            f"          {{{head}}}{{{date}}}\n"
            f"{_bullets(proj.get('bullets', []))}"
        )
    return (
        "\\section{Personal Projects}\n    \\resumeSubHeadingListStart\n"
        + "\n".join(entries)
        + "\n    \\resumeSubHeadingListEnd"
    )


def _education_section(education: list[dict]) -> str:
    entries = []
    for edu in education:
        start, end = edu.get("start"), edu.get("end")
        dates = f"{latex_escape(start)} -- {latex_escape(end)}" if start and end else latex_escape(end or start or "")
        entries.append(
            "    \\resumeSubheading\n"
            f"      {{{latex_escape(edu['institution'])}}}{{{latex_escape(edu.get('location') or '')}}}\n"
            f"      {{{latex_escape(edu['degree'])}}}{{{dates}}}\n"
            f"{_bullets(edu.get('details', []))}"
        )
    return (
        "\\section{Education}\n  \\resumeSubHeadingListStart\n"
        + "\n".join(entries)
        + "\n  \\resumeSubHeadingListEnd"
    )


def _skills_section(skills: list[dict]) -> str:
    lines = " \\\\\n     ".join(
        rf"\textbf{{{latex_escape(g['category'])}}}{{: {latex_escape(', '.join(g['items']))}}}"
        for g in skills
        if g.get("items")
    )
    return (
        "\\section{Technical Skills}\n"
        " \\begin{itemize}[leftmargin=0.15in, label={}]\n"
        "    \\footnotesize{\\item{\n     " + lines + "\n     }}\n"
        " \\end{itemize}"
    )


_SECTION_RENDERERS = {
    "experience": ("experiences", _experience_section),
    "projects": ("projects", _projects_section),
    "education": ("education", _education_section),
    "skills": ("skills", _skills_section),
}

DEFAULT_SECTION_ORDER = ("experience", "projects", "education", "skills")


def render(profile: dict, layout: Layout = DEFAULT_LAYOUT,
           section_order: tuple[str, ...] | list[str] = DEFAULT_SECTION_ORDER) -> str:
    """Profile JSON -> complete LaTeX document. `section_order` is a
    tailoring layout preference; unknown names are ignored and sections
    with no content are skipped either way."""
    sections = [_heading(profile)]
    for name in section_order:
        if name not in _SECTION_RENDERERS:
            continue
        key, renderer = _SECTION_RENDERERS[name]
        if profile.get(key):
            sections.append(renderer(profile[key]))
    body = "\n\n".join(sections)
    return f"{_preamble(layout)}\n\\begin{{document}}\n\n{body}\n\n\\end{{document}}\n"
