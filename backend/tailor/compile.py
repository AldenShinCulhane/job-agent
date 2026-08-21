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
# Tectonic downloads its support bundle during its first compile on a
# machine, which is far slower than any later one. A setup-time check
# must wait that out rather than report a working engine as broken.
FIRST_RUN_TIMEOUT_S = 900


class CompileError(Exception):
    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message)
        self.log_tail = log_tail


@dataclass(frozen=True)
class CompileResult:
    pdf_path: Path
    pages: int
    engine: str


# Last engine that successfully compiled is tried *first* on later
# compiles — a broken tectonic install (observed: bare SIGABRT) must not
# tax every compile with a failed attempt before the pdflatex fallback.
# The remaining engines still run if the cached one fails on a given
# document: an engine can succeed on one input and choke on another.
_working_engine: tuple[str, str] | None = None


def available_engines() -> list[tuple[str, str]]:
    """[(engine name, binary path)] in preference order."""
    found = [(e, shutil.which(e)) for e in ("tectonic", "pdflatex")]
    return [(e, p) for e, p in found if p]


def page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def _run_engine(engine: str, binary: str, tex_path: Path, workdir: Path,
                timeout_s: int = _COMPILE_TIMEOUT_S) -> str:
    if engine == "tectonic":
        commands = [[binary, "--outdir", str(workdir), str(tex_path)]]
    else:
        # pdflatex needs two passes for stable layout/refs.
        base = [binary, "-interaction=nonstopmode", "-halt-on-error",
                f"-output-directory={workdir}", str(tex_path)]
        commands = [base, base]

    output = ""
    for command in commands:
        # Every way an engine can fail must become CompileError, or the
        # fallback loop in compile_tex never reaches the next engine.
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout_s,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            raise CompileError(
                f"{engine} timed out after {timeout_s}s", output[-2000:]
            )
        except OSError as exc:
            raise CompileError(f"{engine} failed to launch: {exc}", output[-2000:])
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise CompileError(f"{engine} failed (exit {proc.returncode})", output[-2000:])
    return output


def compile_with(engine: str, binary: str, tex_path: Path, workdir: Path,
                 jobname: str, timeout_s: int = _COMPILE_TIMEOUT_S) -> CompileResult:
    """One engine, one attempt. Every failure mode — nonzero exit, no
    PDF, unreadable PDF — leaves as CompileError, so callers that iterate
    engines can treat them all the same way."""
    output = _run_engine(engine, binary, tex_path, workdir, timeout_s)
    pdf_path = workdir / f"{jobname}.pdf"
    if not pdf_path.is_file():
        raise CompileError(f"{engine} produced no PDF", output[-2000:])
    try:
        pages = page_count(pdf_path)
    except Exception as exc:  # corrupt output counts as an engine failure
        raise CompileError(
            f"{engine} produced an unreadable PDF: {exc}", output[-2000:]
        ) from exc
    return CompileResult(pdf_path=pdf_path, pages=pages, engine=engine)


def compile_tex(tex_source: str, workdir: Path, jobname: str = "document") -> CompileResult:
    """Compile with the first engine that works (tectonic preferred,
    pdflatex fallback; last known-good engine moves to the front).
    Raises CompileError with the log tail when every engine fails."""
    global _working_engine
    engines = available_engines()
    if _working_engine in engines:
        engines = [_working_engine] + [e for e in engines if e != _working_engine]
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
            result = compile_with(engine, binary, tex_path, workdir, jobname)
        except CompileError as exc:
            errors.append(exc)
            continue
        _working_engine = (engine, binary)
        return result

    summary = "; ".join(str(e) for e in errors)
    raise CompileError(f"all LaTeX engines failed: {summary}", errors[-1].log_tail)
