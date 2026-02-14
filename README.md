# Job Search Automation

Scrapes job listings from [hiring.cafe](https://hiring.cafe), strictly filters them against your search criteria, scores the matches against your resume, then generates tailored resumes and cover letters for your top matches.

## How It Works

```
[1] Scrape       hiring.cafe via headless browser    → .tmp/raw_jobs.json
[2] Parse        normalize, deduplicate, and filter  → .tmp/parsed_jobs.json
[3] Analyze      (optional) LLM enrichment           → .tmp/analyzed_jobs.json
[4] Score        rank jobs against your resume        → .tmp/scored_jobs.json
[5] Generate     tailored resume + cover letter each  → output/applications/
[6] Report       summary with rankings & skill gaps   → output/summary_report.md
```

**Filtering vs. Scoring:** Your search filters (location, title, experience level, salary, etc.) are hard gates — only jobs matching ALL your criteria appear. The match% then ranks those jobs by how well your skills, experience, and education align with what each job requires.

Steps 1-2, 4, and 6 run without any external services. Steps 3 and 5 use [Ollama](https://ollama.com) (free, local LLM).

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

# 3. Create your profile and search config
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

> **What's one-time vs. recurring?** Installing uv, cloning the repo, `uv sync`, `playwright install chromium`, and the setup wizard only need to run once. After that, just run `run_pipeline.py` whenever you want fresh results. Re-run `setup.py` only if you want to change your profile or search filters.

### Manual Setup (Alternative)

Instead of the setup wizard, you can copy and edit the templates directly:

```bash
cp .env.example .env
cp config/user_profile.yaml.example config/user_profile.yaml
cp config/search_filters.yaml.example config/search_filters.yaml

# Then edit each file with your information
```

> **Note:** `.env`, `config/user_profile.yaml`, and `config/search_filters.yaml` are gitignored to protect your personal information and API keys. Only the `.example` templates are tracked.

## LLM (Ollama — Optional)

Steps 3 (Analyze) and 5 (Generate) use [Ollama](https://ollama.com) to enrich job data and write tailored resumes/cover letters. Ollama runs locally — no account or API key needed.

```bash
# Install Ollama: https://ollama.com
# Then pull a model:
ollama pull llama3.3
```

The setup wizard checks if Ollama is running automatically. Without it, the pipeline still scrapes, filters, scores, and generates a summary report — you just won't get the auto-generated application documents.

## Output

```
output/
  summary_report.md                          # Rankings, score distribution, skill gaps
  applications/
    acme-corp_software-engineer/
      resume.docx                            # Tailored resume
      cover_letter.docx                      # Tailored cover letter
      job_details.json                       # Job posting data + match breakdown
```

## Configuration

The setup wizard (`tools/setup.py`) creates these files for you:

| File | What it controls |
|------|-----------------|
| `config/user_profile.yaml` | Your skills, experience, education, projects, and preferences |
| `config/search_filters.yaml` | Search query, locations, experience level, salary range |
| `.env` | Ollama config |

All three are gitignored. Committed `.example` templates show the expected format.

### config/user_profile.yaml

- **personal**: name, email, phone, LinkedIn — used in generated documents
- **skills**: group by category — matched against job requirements
- **experience.total_years**: key factor in experience scoring
- **experience.positions**: your work history with bullet-point achievements
- **projects**: personal or academic projects with descriptions and technologies
- **preferences**: workplace types and preferred locations

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
uv run python tools/run_pipeline.py --skip-scrape --skip-analyze --skip-generate

# Lower the threshold for generated applications (default: 35%)
uv run python tools/run_pipeline.py --skip-scrape --threshold 25

# Update your profile
uv run python tools/setup.py
```

### Run Individual Tools

Each pipeline step can be run standalone:

```bash
uv run python tools/scrape_jobs.py --method browser
uv run python tools/parse_jobs.py --config config/search_filters.yaml
uv run python tools/analyze_jobs.py --batch-size 10
uv run python tools/score_jobs.py
uv run python tools/generate_documents.py --threshold 40 --max-jobs 10
uv run python tools/generate_report.py
```

## Troubleshooting

### "429 Rate Limited" during scraping
The API blocked you. Solutions:
1. Use `--scrape-method browser` for Playwright fallback
2. Wait 5-10 minutes and try again
3. Reduce `pagination.max_pages` in search filters

### "Ollama is not running"
Install Ollama from [ollama.com](https://ollama.com), start it, then pull a model: `ollama pull llama3.3`. Or skip LLM steps with `--skip-analyze --skip-generate`.

### "No jobs found" or all jobs filtered out
- Check search filters — try broader terms or more locations
- Increase `date_filter.days` (try 60 or 90)
- Add more `experience_levels` or `workplace_types`
- If the post-scrape filter removes too many jobs, your search criteria may be stricter than what the API returns — broaden your filters

### Analysis or generation interrupted
Both tools save progress incrementally. Re-run the pipeline with `--skip-scrape` and it will resume from where it left off.

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
  llm_client.py                  # LLM client (Ollama via OpenAI-compatible API)
  scrape_jobs.py                 # Headless browser scraper for hiring.cafe
  parse_jobs.py                  # Normalizes, deduplicates, and filters raw API data
  analyze_jobs.py                # Optional LLM enrichment
  score_jobs.py                  # Deterministic scoring (skills, experience, education)
  generate_documents.py          # Tailored resume/cover letter generator
  generate_report.py             # Summary report with rankings and skill gaps
```

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Chromium (installed via `uv run playwright install chromium`)
- (Optional) [Ollama](https://ollama.com) for LLM features (free, local, no account needed)

## Known Constraints

- hiring.cafe API can rate-limit after heavy use; space runs apart
- Job descriptions over 3000 chars are truncated for LLM analysis
- Playwright requires Chromium installed: `uv run playwright install chromium`
- The API's text search is broad — the post-scrape filter enforces strict title/location matching
