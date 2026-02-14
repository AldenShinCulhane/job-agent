"""
Main orchestrator for the job search automation pipeline.
Chains all tools in sequence with pre-flight validation, cost estimation,
and skip flags for partial reruns.

Usage:
    uv run python tools/run_pipeline.py
    uv run python tools/run_pipeline.py --resume path/to/resume.pdf
    uv run python tools/run_pipeline.py --skip-scrape --threshold 50
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
load_dotenv(PROJECT_ROOT / ".env")


def validate_environment(args, has_profile: bool) -> list:
    """Check that required files, API keys, and dependencies are present. Returns list of issues."""
    issues = []

    # Search config is always required
    if not os.path.exists(args.search_config):
        issues.append(
            f"Search config not found: {args.search_config}\n"
            "    Run 'uv run python tools/setup.py' to create it."
        )

    # User profile is required unless --resume is provided
    if not has_profile and not args.resume:
        issues.append(
            f"User profile not found: {args.user_profile}\n"
            "    Run 'uv run python tools/setup.py' or provide --resume path/to/resume.pdf"
        )

    # Resume validation
    if args.resume and not os.path.exists(args.resume):
        issues.append(f"Resume file not found: {args.resume}")

    # Ollama (required for analyze and generate steps)
    needs_llm = (not args.skip_analyze) or (not args.skip_generate)
    if needs_llm:
        from llm_client import check_ollama
        if not check_ollama():
            issues.append(
                "Ollama is not running. LLM steps (analyze, generate) will fail.\n"
                "    Install: https://ollama.com  |  Pull model: ollama pull llama3.3\n"
                "    Or skip LLM steps: --skip-analyze --skip-generate"
            )

    # Dependencies
    try:
        import requests
    except ImportError:
        issues.append("'requests' package not installed. Run: uv sync")
    try:
        import yaml
    except ImportError:
        issues.append("'pyyaml' package not installed. Run: uv sync")
    try:
        import docx
    except ImportError:
        if not args.skip_generate:
            issues.append("'python-docx' package not installed. Run: uv sync")

    return issues


def run_pipeline(args):
    """Execute the full pipeline."""
    start_time = time.time()

    print("=" * 60)
    print("  WAT Job Search Automation Pipeline")
    print("=" * 60)

    has_profile = os.path.exists(args.user_profile)
    has_search_config = os.path.exists(args.search_config)

    # Auto-trigger setup if needed (but not if --resume covers the profile)
    if not has_search_config:
        # Search config is always required — trigger setup
        print("\nNo search config found. Starting setup wizard...\n")
        from setup import interactive_setup
        interactive_setup()
        has_search_config = os.path.exists(args.search_config)
        has_profile = os.path.exists(args.user_profile)
        if not has_search_config:
            print("\nSetup did not create search config. Exiting.")
            sys.exit(1)
    elif not has_profile and not args.resume:
        # No profile and no resume — trigger setup
        print("\nNo user profile found. Starting setup wizard...\n")
        from setup import interactive_setup
        interactive_setup()
        has_profile = os.path.exists(args.user_profile)
        if not has_profile:
            print("\nSetup did not create a profile. Exiting.")
            sys.exit(1)

    resume_only_mode = args.resume and not has_profile
    if resume_only_mode:
        print(f"\n  Resume mode: using '{args.resume}' (no user profile — scoring will be skipped)")

    # Pre-flight validation
    print("\n[0/6] Validating environment...")
    issues = validate_environment(args, has_profile)
    if issues:
        print("  ERRORS:")
        for issue in issues:
            print(f"    - {issue}")
        print("\nFix the issues above and try again.")
        sys.exit(1)
    print("  All checks passed.\n")

    # Step 1: Scrape
    raw_jobs_path = str(PROJECT_ROOT / ".tmp" / "raw_jobs.json")
    if args.skip_scrape:
        if os.path.exists(raw_jobs_path):
            print("[1/6] SKIPPED: Using cached raw_jobs.json")
        else:
            print("[1/6] ERROR: --skip-scrape but .tmp/raw_jobs.json doesn't exist.")
            print("  Run without --skip-scrape first to scrape jobs.")
            sys.exit(1)
    else:
        print("[1/6] Scraping jobs from hiring.cafe...")
        print("  This may take 1-2 minutes (browser needs to bypass security check)...")
        from scrape_jobs import scrape_jobs
        jobs = scrape_jobs(args.search_config, args.scrape_method, raw_jobs_path)
        if not jobs:
            print("\n  ERROR: No jobs were scraped.")
            print("  Possible causes:")
            print("    - hiring.cafe may be down or rate-limiting you")
            print("    - Your search query may be too specific")
            print("    - Try again in a few minutes, or use --scrape-method browser")
            sys.exit(1)
        print(f"  Done: {len(jobs):,} jobs scraped.")

    # Step 2: Parse + filter
    parsed_jobs_path = str(PROJECT_ROOT / ".tmp" / "parsed_jobs.json")
    print("\n[2/6] Parsing, filtering, and normalizing job data...")
    import yaml
    with open(args.search_config, "r", encoding="utf-8") as f:
        search_config = yaml.safe_load(f)
    from parse_jobs import parse_jobs
    parsed = parse_jobs(raw_jobs_path, parsed_jobs_path, config=search_config)

    if not parsed:
        print("\n  ERROR: No jobs remaining after filtering.")
        print("  Your search filters may be too strict. Try:")
        print("    - Broadening your search query")
        print("    - Adding more locations or workplace types")
        print("    - Increasing experience levels")
        print("    - Removing the salary minimum")
        print("  Edit config/search_filters.yaml or re-run: uv run python tools/setup.py")
        sys.exit(1)
    print(f"  Done: {len(parsed):,} jobs after filtering.")

    # Step 3: Analyze (LLM)
    analyzed_jobs_path = str(PROJECT_ROOT / ".tmp" / "analyzed_jobs.json")
    if args.skip_analyze:
        if os.path.exists(analyzed_jobs_path):
            print("\n[3/6] SKIPPED: Using cached analyzed_jobs.json")
        else:
            print("\n[3/6] SKIPPED: No cached analysis found — using parsed data for scoring.")
    else:
        print(f"\n[3/6] Analyzing {len(parsed):,} jobs with Ollama...")
        batch_size = args.batch_size
        est_calls = (len(parsed) + batch_size - 1) // batch_size
        print(f"  Estimated API calls: ~{est_calls} (batches of {batch_size})")

        if not args.yes:
            confirm = input("  Proceed with analysis? [Y/n] ").strip().lower()
            if confirm == "n":
                print("  Skipping analysis. Use --skip-analyze to reuse cached data.")
                sys.exit(0)

        from analyze_jobs import analyze_jobs
        analyzed = analyze_jobs(parsed_jobs_path, analyzed_jobs_path, batch_size)
        print(f"  Done: {len(analyzed):,} jobs analyzed.")

    # Step 4: Score
    scored_jobs_path = str(PROJECT_ROOT / ".tmp" / "scored_jobs.json")
    if resume_only_mode:
        # No profile to score against — copy parsed/analyzed as "scored" with neutral scores
        print(f"\n[4/6] SKIPPED: No user profile for scoring (resume-only mode).")
        score_input = analyzed_jobs_path if os.path.exists(analyzed_jobs_path) else parsed_jobs_path
        with open(score_input, "r", encoding="utf-8") as f:
            jobs_data = json.load(f)
        # Add neutral scores so downstream steps work
        for job in jobs_data:
            job["match_score"] = 50.0
            job["match_breakdown"] = {"skills": 50.0, "experience": 50.0, "education": 50.0}
            job["match_reasons"] = ["Resume-only mode — no profile scoring"]
            job["gap_reasons"] = []
            job["matched_skills"] = []
            job["missing_skills"] = []
        os.makedirs(os.path.dirname(scored_jobs_path), exist_ok=True)
        with open(scored_jobs_path, "w", encoding="utf-8") as f:
            json.dump(jobs_data, f, indent=2, ensure_ascii=False)
        print(f"  {len(jobs_data):,} jobs passed through with neutral scores.")
    else:
        # Score from analyzed if available, otherwise from parsed
        score_input = analyzed_jobs_path if os.path.exists(analyzed_jobs_path) else parsed_jobs_path
        print(f"\n[4/6] Scoring jobs against your profile...")
        from score_jobs import score_jobs
        scored = score_jobs(score_input, args.user_profile, scored_jobs_path)
        if scored:
            top_score = scored[0].get("match_score", 0) if scored else 0
            print(f"  Done: {len(scored):,} jobs scored (top match: {top_score}%).")

    # Step 5: Generate documents (LLM)
    if args.skip_generate:
        print("\n[5/6] SKIPPED: Document generation")
    else:
        with open(scored_jobs_path, "r") as f:
            scored_data = json.load(f)
        qualifying = [j for j in scored_data if j.get("match_score", 0) >= args.threshold]
        qualifying = qualifying[:args.max_applications]

        if not qualifying:
            print(f"\n[5/6] No jobs scoring {args.threshold}%+. Skipping document generation.")
            print("  Tip: Lower the threshold with --threshold or adjust your profile.")
        else:
            print(f"\n[5/6] Generating applications for {len(qualifying)} jobs...")
            est_calls = len(qualifying) * 2
            print(f"  Estimated API calls: ~{est_calls} (resume + cover letter each)")

            proceed = True
            if not args.yes:
                confirm = input("  Proceed with generation? [Y/n] ").strip().lower()
                if confirm == "n":
                    print("  Skipping generation.")
                    proceed = False

            if proceed:
                from generate_documents import generate_documents
                generate_documents(
                    jobs_path=scored_jobs_path,
                    profile_path=args.user_profile if has_profile else None,
                    base_resume_path=args.resume,
                    output_dir=str(PROJECT_ROOT / "output"),
                    threshold=args.threshold,
                    max_jobs=args.max_applications,
                )
                print(f"  Done: applications generated for {len(qualifying)} jobs.")

    # Step 6: Report
    if has_profile:
        print(f"\n[6/6] Generating summary report...")
        from generate_report import generate_report
        generate_report(
            jobs_path=scored_jobs_path,
            config_path=args.search_config,
            profile_path=args.user_profile,
            output_path=str(PROJECT_ROOT / "output" / "summary_report.md"),
        )
        print(f"  Done: output/summary_report.md")
    else:
        print(f"\n[6/6] SKIPPED: Summary report (no user profile for skill gap analysis)")

    # Final summary
    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"  Pipeline complete in {elapsed:.0f}s")
    print(f"{'=' * 60}")
    if has_profile:
        print(f"  Report:       output/summary_report.md")
    print(f"  Applications: output/applications/")
    print(f"  Raw data:     .tmp/")
    if not has_profile:
        print(f"\n  Tip: Run 'uv run python tools/setup.py' to create a profile")
        print(f"  for personalized scoring and skill gap analysis.")


def main():
    parser = argparse.ArgumentParser(
        description="WAT Job Search Automation Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python tools/run_pipeline.py                           # Full pipeline
  uv run python tools/run_pipeline.py --resume my_resume.pdf    # Use resume (no profile needed)
  uv run python tools/run_pipeline.py --skip-scrape             # Re-score without re-scraping
  uv run python tools/run_pipeline.py --yes --max-applications 10
        """,
    )

    parser.add_argument("--search-config",
                        default=str(PROJECT_ROOT / "config" / "search_filters.yaml"),
                        help="Path to search filters YAML")
    parser.add_argument("--user-profile",
                        default=str(PROJECT_ROOT / "config" / "user_profile.yaml"),
                        help="Path to user profile YAML")
    parser.add_argument("--resume", default=None,
                        help="Path to base resume file (.txt, .md, .docx, .pdf)")
    parser.add_argument("--scrape-method", choices=["api", "browser", "auto"], default="auto",
                        help="Scraping method (default: auto)")
    parser.add_argument("--threshold", type=float, default=35.0,
                        help="Minimum match score for application generation (default: 35)")
    parser.add_argument("--max-applications", type=int, default=20,
                        help="Maximum number of applications to generate (default: 20)")
    parser.add_argument("--batch-size", type=int, default=5,
                        help="Jobs per LLM analysis batch (default: 5)")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Skip scraping, use cached .tmp/raw_jobs.json")
    parser.add_argument("--skip-analyze", action="store_true",
                        help="Skip LLM analysis, use cached .tmp/analyzed_jobs.json")
    parser.add_argument("--skip-generate", action="store_true",
                        help="Skip document generation (score only)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompts")

    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
