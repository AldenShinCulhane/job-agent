"""
LLM-powered job analysis. Enriches parsed jobs with deeper insights using
the configured LLM provider. Since hiring.cafe's v5_processed_job_data
already provides structured skills and requirements, this tool focuses on:
role summary, company type classification, red flags, and culture signals.

Can be skipped if the v5 data is sufficient for scoring.
"""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from llm_client import chat_completion

BATCH_SIZE = 5

ANALYSIS_PROMPT = """Analyze each job posting and extract structured information. Return a JSON array where each element corresponds to one job (in the same order as provided).

For each job, extract:
- required_skills: list of hard technical skills explicitly required (supplement the already-extracted skills if any are missing)
- preferred_skills: list of skills mentioned as nice-to-have or preferred
- years_experience_required: integer or null if not specified
- education_requirement: string like "Bachelor's in CS" or null
- company_type: one of "startup", "enterprise", "agency", "nonprofit", "government", "unknown"
- role_summary: 2-3 sentence summary of what this role does
- key_responsibilities: list of top 5 responsibilities
- red_flags: list of any concerning aspects (e.g., "unpaid trial", "unlimited PTO but high expectations")
- culture_signals: list of culture indicators (e.g., "fast-paced", "collaborative", "remote-first")

Return ONLY valid JSON. No markdown fences, no explanation."""


def build_batch_prompt(jobs: list) -> str:
    parts = []
    for i, job in enumerate(jobs):
        parts.append(
            f"--- JOB {i + 1} ---\n"
            f"Title: {job.get('title', 'N/A')}\n"
            f"Company: {job.get('company', 'N/A')}\n"
            f"Location: {job.get('location', 'N/A')}\n"
            f"Experience Level: {job.get('experience_level', 'N/A')}\n"
            f"Already-extracted skills: {', '.join(job.get('skills', []))}\n"
            f"Requirements summary: {job.get('requirements_summary', 'N/A')}\n"
            f"Description:\n{job.get('description_text', 'N/A')[:3000]}\n"
        )
    return "\n".join(parts)


def analyze_batch(jobs: list) -> list:
    user_msg = build_batch_prompt(jobs)

    try:
        text = chat_completion(
            system=ANALYSIS_PROMPT,
            user_message=user_msg,
            max_tokens=4096,
            task="analyze",
        )
    except Exception as e:
        print(f"    LLM error: {e}")
        return [empty_analysis() for _ in jobs]

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        analyses = json.loads(text)
        if isinstance(analyses, dict):
            analyses = [analyses]
        return analyses
    except json.JSONDecodeError:
        print(f"    Failed to parse LLM response as JSON. Response: {text[:200]}")
        return [empty_analysis() for _ in jobs]


def empty_analysis() -> dict:
    return {
        "required_skills": [],
        "preferred_skills": [],
        "years_experience_required": None,
        "education_requirement": None,
        "company_type": "unknown",
        "role_summary": "",
        "key_responsibilities": [],
        "red_flags": [],
        "culture_signals": [],
    }


def analyze_jobs(input_path: str = None, output_path: str = None, batch_size: int = BATCH_SIZE) -> list:
    """Analyze all parsed jobs using the configured LLM. Saves progress incrementally."""
    if input_path is None:
        input_path = str(PROJECT_ROOT / ".tmp" / "parsed_jobs.json")
    if output_path is None:
        output_path = str(PROJECT_ROOT / ".tmp" / "analyzed_jobs.json")

    progress_path = output_path.replace(".json", "_progress.json")

    with open(input_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    # Resume from progress if available
    analyzed = []
    start_idx = 0
    if os.path.exists(progress_path):
        with open(progress_path, "r", encoding="utf-8") as f:
            analyzed = json.load(f)
        start_idx = len(analyzed)
        if start_idx > 0:
            print(f"Resuming from batch {start_idx // batch_size + 1} ({start_idx} jobs already analyzed)")

    if start_idx >= len(jobs):
        print("All jobs already analyzed.")
        # Save final output
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(analyzed, f, indent=2, ensure_ascii=False)
        return analyzed

    remaining = jobs[start_idx:]
    total_batches = (len(remaining) + batch_size - 1) // batch_size
    print(f"Analyzing {len(remaining):,} jobs in {total_batches} batches (~{total_batches} API calls)")

    for batch_num in range(0, len(remaining), batch_size):
        batch = remaining[batch_num : batch_num + batch_size]
        batch_idx = (start_idx + batch_num) // batch_size + 1
        print(f"  Batch {batch_idx}/{(start_idx // batch_size) + total_batches}: analyzing {len(batch)} jobs...")

        analyses = analyze_batch(batch)

        for i, job in enumerate(batch):
            analysis = analyses[i] if i < len(analyses) else empty_analysis()
            enriched = dict(job)
            enriched["analysis"] = analysis
            analyzed.append(enriched)

        # Save progress
        os.makedirs(os.path.dirname(progress_path), exist_ok=True)
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(analyzed, f, indent=2, ensure_ascii=False)

        time.sleep(0.5)

    # Save final output and clean up
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(analyzed, f, indent=2, ensure_ascii=False)

    if os.path.exists(progress_path):
        os.remove(progress_path)

    print(f"Analyzed {len(analyzed):,} jobs -> {output_path}")
    return analyzed


def main():
    parser = argparse.ArgumentParser(description="Analyze jobs with LLM")
    parser.add_argument("--input", default=str(PROJECT_ROOT / ".tmp" / "parsed_jobs.json"))
    parser.add_argument("--output", default=str(PROJECT_ROOT / ".tmp" / "analyzed_jobs.json"))
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    analyze_jobs(args.input, args.output, args.batch_size)


if __name__ == "__main__":
    main()
