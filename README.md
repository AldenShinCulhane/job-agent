# Job Search Automation

Scrapes job listings from [hiring.cafe](https://hiring.cafe), strictly filters them against your search criteria, scores the matches against your resume, then generates tailored resumes and cover letters for your top matches.

## How It Works

```
[1] Scrape       hiring.cafe via headless browser    → .tmp/raw_jobs.json
[2] Parse        normalize, deduplicate, and filter  → .tmp/parsed_jobs.json
[3] Score        rank jobs against your profile       → .tmp/scored_jobs.json
[4] Select       pick how many to apply to            → .tmp/selected_jobs.json
[5] Generate     LLM analysis + tailored docs         → output/applications/
[6] Report       summary with rankings & skill gaps   → output/summary_report.md
```

**Filtering vs. Scoring:** Your search filters (location, title, experience level, salary, etc.) are hard gates — only jobs matching ALL your criteria appear. The match% then ranks those jobs by how well your skills, experience, and education align with what each job requires.

Steps 1-3 and 6 run without any external services. Step 5 uses a free LLM API — [SambaNova](https://cloud.sambanova.ai/apis) (unlimited, no credit card).

## Quick Start

### First-Time Setup (do once)

```bash
# 1. Install uv (Python package manager) — skip if already installed

# macOS / Linux / WSL:
curl -LsSf https://astral.sh/uv/install.sh | sh
# Then restart your terminal, or add uv to your current session:
#   export PATH="$HOME/.local/bin:$PATH"

# Windows (PowerShell):
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Then restart your terminal.

# 2. Clone and install dependencies
git clone <your-repo-url>
cd job-agent
uv sync                              # installs Python packages (one time)
uv run playwright install chromium   # installs browser for scraping (one time)

# 3. (Optional) Install a LaTeX distribution for auto-compiled PDF resumes
#    Windows: download from https://miktex.org/download
#    macOS:   brew install --cask mactex-no-gui
#    Linux:   sudo apt install texlive-full
#    Without this, resumes are saved as .tex files you can compile manually.

# 4. Create your profile and search config
uv run python tools/setup.py
```

### Run the Pipeline (do each time)

```bash
uv run python tools/run_pipeline.py
```

Or with an existing resume for better results:
```bash
uv run python tools/run_pipeline.py --resume path/to/resume.pdf
```

**Resume-only mode** — skip profile setup entirely and just use your resume:
```bash
# Only needs search_filters.yaml (the setup wizard creates it)
uv run python tools/run_pipeline.py --resume path/to/resume.pdf
```
In this mode, scoring is skipped (no profile to score against) but jobs are still scraped, filtered, and application documents are generated from your resume.

Results appear in:
- `output/summary_report.md` — score rankings and skill gaps
- `output/applications/` — tailored resume + cover letter per job

> **What's one-time vs. recurring?** Installing uv, cloning the repo, `uv sync`, `playwright install chromium`, MiKTeX, and the setup wizard only need to run once. After that, just run `run_pipeline.py` whenever you want fresh results. Re-run `setup.py` only if you want to change your profile or search filters.

### Manual Setup (Alternative)

Instead of the setup wizard, you can copy and edit the templates directly:

```bash
cp .env.example .env
cp config/user_profile.yaml.example config/user_profile.yaml
cp config/search_filters.yaml.example config/search_filters.yaml

# Then edit each file with your information
```

> **Note:** `.env`, `config/user_profile.yaml`, and `config/search_filters.yaml` are gitignored to protect your personal information and API keys. Only the `.example` templates are tracked.

## LLM (SambaNova — Free)

Step 5 uses an LLM for job analysis and tailored resume/cover letter generation.

1. Go to [cloud.sambanova.ai/apis](https://cloud.sambanova.ai/apis)
2. Create a free API key (no credit card required)
3. Add it to your `.env`: `SAMBANOVA_API_KEY=...`

SambaNova provides unlimited free tokens at 20 requests/min — no daily cap. The setup wizard handles this automatically. Without an API key, the pipeline still scrapes, filters, scores, and generates a summary report — you just won't get the auto-generated application documents.

**Alternative providers** (use `--provider` flag): Cerebras, Gemini, Groq. Set the corresponding API key in `.env` and pass `--provider cerebras` (etc.) when running the pipeline.

## Output

```
output/
  summary_report.md                          # Rankings, score distribution, skill gaps
  applications/
    acme-corp_software-engineer/
      resume.pdf                             # Tailored resume (auto-compiled from LaTeX)
      resume.tex                             # LaTeX source (compile manually if no pdflatex)
      cover_letter.pdf                       # Tailored cover letter
      cover_letter.md                        # Cover letter source text
      job_details.json                       # Job posting data + match breakdown
```

On re-runs, previous files are renamed with `_old` suffix (e.g. `resume_old.pdf`) so you can compare versions.

## Configuration

The setup wizard (`tools/setup.py`) creates these files for you:

| File | What it controls |
|------|-----------------|
| `config/user_profile.yaml` | Your skills, experience, education, projects |
| `config/search_filters.yaml` | Search query, locations, experience level, salary range |
| `.env` | LLM API key (SambaNova by default) |

All three are gitignored. Committed `.example` templates show the expected format.

### config/user_profile.yaml

- **personal**: name, email, phone, location, LinkedIn, GitHub — used in generated documents
- **skills**: group by category — matched against job requirements
- **experience.total_years**: key factor in experience scoring
- **experience.positions**: your work history with bullet-point achievements (optional `location` per position)
- **education**: degree, institution, graduation year (optional `start_year`, `honors`, `location`)
- **projects**: personal or academic projects with highlights and technologies

### config/search_filters.yaml

- **search.query**: main search term (e.g., "developer") — also filters job titles
- **locations**: cities/regions in hiring.cafe format with coordinates
- **workplace_types**: Remote, Hybrid, Onsite (select all that apply)
- **experience_levels**: Entry Level, Mid Level, Senior Level, etc. (select all that apply)
- **salary.min_annual**: minimum yearly salary filter
- **commitment_types**: Full Time, Part Time, Contract, etc.
- **date_filter.days**: how recent (default: 30 days)

## Common Operations

```bash
# Re-score without re-scraping (uses cached job data)
uv run python tools/run_pipeline.py --skip-scrape

# Resume-only mode — no profile setup needed (just search filters)
uv run python tools/run_pipeline.py --resume my_resume.pdf

# Score only — no LLM calls at all
uv run python tools/run_pipeline.py --skip-scrape --skip-generate

# Non-interactive (auto-selects top 5)
uv run python tools/run_pipeline.py --skip-scrape --yes

# Use a different LLM provider
uv run python tools/run_pipeline.py --provider cerebras

# Update your profile
uv run python tools/setup.py
```

### Run Individual Tools

Each pipeline step can be run standalone:

```bash
uv run python tools/scrape_jobs.py --method browser
uv run python tools/parse_jobs.py --config config/search_filters.yaml
uv run python tools/score_jobs.py
uv run python tools/analyze_jobs.py --batch-size 10
uv run python tools/generate_documents.py --threshold 40 --max-jobs 10
uv run python tools/generate_report.py
```

## Troubleshooting

### "429 Rate Limited" during scraping
The API blocked you. Solutions:
1. Use `--scrape-method browser` for Playwright fallback
2. Wait 5-10 minutes and try again
3. Reduce `pagination.max_pages` in search filters

### "No API key for SambaNova"
Get a free key at [cloud.sambanova.ai/apis](https://cloud.sambanova.ai/apis) and add `SAMBANOVA_API_KEY=...` to `.env`. Or use `--provider` to select a different provider, or `--skip-generate` to skip LLM steps entirely.

### "No jobs found" or all jobs filtered out
- Check search filters — try broader terms or more locations
- Increase `date_filter.days` (try 60 or 90)
- Add more `experience_levels` or `workplace_types`
- If the post-scrape filter removes too many jobs, your search criteria may be stricter than what the API returns — broaden your filters

### Analysis or generation interrupted
Both tools save progress incrementally. Re-run the pipeline with `--skip-scrape` and it will resume from where it left off.

### "pdflatex not found"
Resumes are generated as LaTeX (.tex) and auto-compiled to PDF. If pdflatex is not installed, the .tex file is saved and you can compile it manually. The pipeline auto-detects MiKTeX on Windows even if it's not in your PATH. To install:
- **Windows:** Install [MiKTeX](https://miktex.org/download), then restart your terminal. If still not found, add MiKTeX to your PATH: `setx PATH "%PATH%;%LOCALAPPDATA%\Programs\MiKTeX\miktex\bin\x64"`
- **macOS:** `brew install --cask mactex-no-gui`
- **Linux:** `sudo apt install texlive-full`

### Low match scores across the board
- Review your `config/user_profile.yaml` — are all your skills listed?
- Match scores reflect skill/experience/education fit, not filter criteria
- Consider whether the roles you're finding genuinely match your background

## Project Structure

```
config/
  user_profile.yaml.example      # Template — copy and fill in your info
  search_filters.yaml.example    # Template — copy and adjust search criteria
tools/
  setup.py                       # Interactive setup wizard
  run_pipeline.py                # Main entry point — runs all steps
  llm_client.py                  # LLM client (SambaNova default, --provider for alternatives)
  scrape_jobs.py                 # Headless browser scraper for hiring.cafe
  parse_jobs.py                  # Normalizes, deduplicates, and filters raw API data
  analyze_jobs.py                # Optional LLM enrichment
  score_jobs.py                  # Deterministic scoring (skills, experience, education)
  generate_documents.py          # LaTeX resume + DOCX cover letter generator
  generate_report.py             # Summary report with rankings and skill gaps
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Chromium (installed via `uv run playwright install chromium`)
- (Optional) [MiKTeX](https://miktex.org/download) or TeX Live for PDF resume compilation
- (Optional) [SambaNova](https://cloud.sambanova.ai/apis) API key for LLM features (free, unlimited)

## Known Constraints

- hiring.cafe API can rate-limit after heavy use; space runs apart
- Job descriptions over 1,500 chars are truncated for LLM analysis
- Playwright requires Chromium installed: `uv run playwright install chromium`
- The API's text search is broad — the post-scrape filter enforces strict title/location matching
- Resumes and cover letters are each limited to 1 page (auto-trimmed if needed)
- Processing 5 jobs requires ~12 LLM calls (2 analysis + 5 resume + 5 cover letter). Selecting more jobs increases this linearly.
- SambaNova free tier: unlimited tokens, 20 requests/min — comfortably handles all pipeline operations
