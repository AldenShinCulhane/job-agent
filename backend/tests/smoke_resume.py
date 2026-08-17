"""Live resume round-trip (uses the Claude subscription; run manually).

    uv run python -m backend.tests.smoke_resume <resume.pdf|docx> [--max-pages N]

Proves the M1 criterion: a real resume round-trips to a compiled PDF in
our template within the page limit, with correct content and a populated
truth ledger.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from backend.core import db
from backend.resume import facts, ingest, template
from backend.tailor import compile as latex


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    resume_path = Path(args[0]).expanduser()
    max_pages = 1
    if "--max-pages" in sys.argv:
        max_pages = int(sys.argv[sys.argv.index("--max-pages") + 1])

    db.init()

    text, method = ingest.extract_text(resume_path)
    print(f"text extraction: {method}, {len(text)} chars")

    profile = await ingest.extract_profile(text)
    print(
        f"extracted: {len(profile['experiences'])} experiences, "
        f"{len(profile['projects'])} projects, {len(profile['education'])} education, "
        f"{len(profile['skills'])} skill groups"
    )

    ledger = facts.build_ledger(profile)
    bullets = sum(1 for e in ledger.values() if e["kind"] == "bullet")
    print(f"truth ledger: {len(ledger)} facts ({bullets} bullets)")
    assert ledger, "ledger must be populated"

    with tempfile.TemporaryDirectory() as tmp:
        result = latex.compile_tex(template.render(profile), Path(tmp), jobname="resume")
        print(f"compiled with {result.engine}: {result.pages} page(s)")
        keep = db.DATA_DIR / "smoke_resume.pdf"
        keep.write_bytes(result.pdf_path.read_bytes())
        print(f"saved preview to {keep}")
        assert result.pages <= max_pages, f"{result.pages} pages > limit {max_pages}"

    print("SMOKE PASS — round-trip complete")


if __name__ == "__main__":
    asyncio.run(main())
