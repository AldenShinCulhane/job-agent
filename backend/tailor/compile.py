"""LaTeX -> PDF: engine detection, compilation, page count.

Tectonic preferred (single binary, auto-fetches packages), pdflatex
fallback. The engine actually used is returned so provenance can record
it — the two engines can paginate differently, so page-limit decisions
are only valid for the engine that made them.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

_COMPILE_TIMEOUT_S = 120


class CompileError(Exception):
    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.log_tail = log_tail


@dataclass(frozen=True)
class CompileResult:
    pdf_path: Path
    pages: int
    engine: str


# First engine that successfully compiles is reused for the process
# lifetime — a broken tectonic install (observed: bare SIGABRT) must not
# tax every compile with a failed attempt before the pdflatex fallback.
_working_engine: tuple[str, str] | None = None


def available_engines() -> list[tuple[str, str]]:
    """[(engine name, binary path)] in preference order."""
    found = [(e, shutil.which(e)) for e in ("tectonic", "pdflatex")]
    return [(e, p) for e, p in found if p]


def page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def _run_engine(engine: str, binary: str, tex_path: Path, workdir: Path) -> str:
    if engine == "tectonic":
        commands = [[binary, "--outdir", str(workdir), str(tex_path)]]
    else:
        # pdflatex needs two passes for stable layout/refs.
        base = [binary, "-interaction=nonstopmode", "-halt-on-error",
                f"-output-directory={workdir}", str(tex_path)]
        commands = [base, base]

    output = ""
    for command in commands:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=_COMPILE_TIMEOUT_S,
            cwd=workdir,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise CompileError(f"{engine} failed (exit {proc.returncode})", output[-2000:])
    return output


def compile_tex(tex_source: str, workdir: Path, jobname: str = "document") -> CompileResult:
    """Compile with the first engine that works (tectonic preferred,
    pdflatex fallback). Raises CompileError with the log tail when every
    engine fails."""
    global _working_engine
    engines = [_working_engine] if _working_engine else available_engines()
    if not engines:
        raise CompileError(
            "No LaTeX engine found — install tectonic (brew install tectonic) "
            "or a TeX distribution providing pdflatex."
        )

    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{jobname}.tex"
    tex_path.write_text(tex_source)

    errors: list[CompileError] = []
    for engine, binary in engines:
        try:
            output = _run_engine(engine, binary, tex_path, workdir)
        except CompileError as exc:
            errors.append(exc)
            continue
        pdf_path = workdir / f"{jobname}.pdf"
        if not pdf_path.is_file():
            errors.append(CompileError(f"{engine} produced no PDF", output[-2000:]))
            continue
        _working_engine = (engine, binary)
        return CompileResult(pdf_path=pdf_path, pages=page_count(pdf_path), engine=engine)

    summary = "; ".join(str(e) for e in errors)
    raise CompileError(f"all LaTeX engines failed: {summary}", errors[-1].log_tail)
