"""Cover letter LaTeX render (v1 template ported to structured input).

The LLM supplies paragraphs + closing as validated JSON; everything else
— contact header, greeting, signature — is rendered by this module from
the profile and job rows, escaped. Layout knobs mirror template.Layout
so page_limit.py can tighten within readability floors.
"""

from dataclasses import dataclass

from backend.resume.template import latex_escape


@dataclass(frozen=True)
class CoverLayout:
    """Tunable cover-letter layout. Defaults are the proven v1 values;
    page_limit.py may tighten them within readability floors."""

    font_pt: int = 11
    margin_in: float = 1.0
    par_skip_pt: int = 6


DEFAULT_COVER_LAYOUT = CoverLayout()


def render_cover(profile: dict, job: dict, paragraphs: list[dict],
                 closing: str, layout: CoverLayout = DEFAULT_COVER_LAYOUT) -> str:
    """Structured cover-letter content -> complete LaTeX document."""
    contact = profile.get("contact") or {}
    header_lines = [latex_escape(profile["name"])]
    for field in ("location", "email", "phone", "linkedin", "github"):
        value = contact.get(field)
        if value:
            header_lines.append(latex_escape(value))

    body = "\n\n".join(latex_escape(p["text"].strip()) for p in paragraphs)
    greeting = f"Dear {latex_escape(job.get('company') or 'Hiring')} Hiring Team,"
    signature = (
        f"{latex_escape(closing.strip())} \\\\\n{latex_escape(profile['name'])}"
    )

    return rf"""\documentclass[{layout.font_pt}pt,letterpaper]{{article}}
\usepackage[margin={layout.margin_in}in]{{geometry}}
\usepackage{{parskip}}
\setlength{{\parskip}}{{{layout.par_skip_pt}pt}}
\usepackage[hidelinks]{{hyperref}}
\pagestyle{{empty}}
\begin{{document}}
\noindent
{" \\\\".join(header_lines)}
\vspace{{1em}}

{greeting}

{body}

{signature}

\end{{document}}
"""
