# Retained verification logs

Nine logs. Each one names, in its own provenance line, the commit whose
code it exercised — read that line, not this table, if the two ever
disagree. Seven are current evidence and bind to `a3f85d2`. Two are kept
from earlier rounds precisely because they FAILED: they are the proof
that these checks can fail.

Every log is a real run. None was hand-edited beyond stripping terminal
escapes (the scripts export `TERM=dumb` for the same reason).

## Current evidence — code commit `a3f85d2`

| Log | What it establishes | Scope it does NOT establish |
|---|---|---|
| `ubuntu-freshclone-20260821.log` | Fresh LOCAL clone of the host checkout in a clean `ubuntu:24.04` container on aarch64: every dependency fetched inside it, Tectonic 0.17.0 typesets both templates, `latex_engines: ["tectonic"]`, `.data` `drwx------`, real Chromium 151, 216 tests. | Not a remote clone. Not x86_64. |
| `macos-cleanenv-freshclone-20260821-PASS.log` | Clean ENVIRONMENT on macOS — empty `HOME`, no Homebrew on `PATH`, uv/Node/npm/Chromium/Tectonic all fetched from upstream — with Tectonic as the only engine, 216 tests. | Not a pristine machine and not an ephemeral runner: the kernel, frameworks and Xcode CLT are the host's. The OCR stack is deliberately absent, so nothing here is evidence about the OCR fallback path. |
| `macos-freshclone-20260821.log` | Fresh CHECKOUT built with the host toolchain, booted through the documented `uv run job-agent` entrypoint, `latex_engines: ["tectonic","pdflatex"]`, 217 passed + 1 skipped. | Not a bare-machine first install — the container run covers that. |
| `setup-macos-both-engines-20260821.log` | `setup.sh` on a host with both engines: it test-compiles both templates, reports this host's Tectonic 0.16.9 as FAILED, pdflatex as ok, and continues green because one engine works. | — |
| `setup-macos-tectonic-only-20260821.log` | The same host with pdflatex hidden behind a `PATH` of Homebrew symlinks: setup warns that no installed engine can typeset the templates and closes with NO WORKING LaTeX ENGINE. | The engine is hidden, not uninstalled; this is the machine's behaviour, not a second machine. |
| `engines-geometry-fonts-20260821.log` | Both engines side by side on the sample resume and a sample cover letter: content box, last-page fullness under this branch's algorithm and the pre-`11edbca` one, every required text fragment read back from the text layer, and `pdffonts`. | `pdffonts` reports `uni=yes` for the three TeXGyreHeros text faces under Tectonic and `uni=no` for both FontAwesome CID fonts, which carry the contact icons. "uni=yes" is not true of that PDF unqualified. |
| `upload-retry-browser-check-20260821.log` | Real Chromium against the built bundle from an isolated checkout pinned to the commit, served statically: after a failed upload the file input is cleared, so re-picking the SAME file retries. | The app backend is never started, so this says nothing about server behaviour. |

The test count differs by machine on purpose: the text-layer tests run
once per installed engine, so a one-engine machine collects 216 and a
two-engine machine collects 218, one of which skips (this host's Tectonic
0.16.9 cannot typeset the resume template).

## Kept because they failed

| Log | Code commit | What failed |
|---|---|---|
| `macos-cleanenv-freshclone-20260821-uvpath-FAIL.log` | `5bcd317` | `setup.sh` installed uv into `~/.local/bin`, which the current shell could not see, so the next documented command died with `uv: command not found`. |
| `macos-cleanenv-freshclone-20260821-tectonic-FAIL.log` | `b4aae5c` | The resume template used pdfTeX-only primitives, so Tectonic aborted and a Tectonic-only machine could produce no PDFs at all. |

## Regenerating

```bash
docker run --rm -v "$PWD":/src:ro -v "$PWD/scripts/ci":/scratch:ro \
  ubuntu:24.04 bash /scratch/ubuntu_freshclone.sh
scripts/ci/macos_cleanenv_freshclone.sh ~/.cache
scripts/ci/macos_freshclone.sh ~/.cache          # needs port 8700 free
./.venv/bin/python scripts/ci/upload_retry_browser_check.py
./.venv/bin/python scripts/ci/engine_text_evidence.py \
  --engine pdflatex=pdflatex --engine tectonic=/path/to/tectonic-0.17
bash scripts/setup.sh                            # the two setup logs
```

Write the output to a scratch path first and copy it in afterwards:
generating a log inside the worktree makes the run's own provenance line
report the tree as dirty.
