"""
ReAct agent for the Agentic Job Search Pipeline.

Uses a Reason-Act-Observe loop: the LLM reasons about what step to take,
calls a tool, reads the result, and decides next. Existing pipeline scripts
become "tools" the agent invokes — they stay unchanged.

LLM calls go through llm_client.py which automatically selects the best
available provider and fails over to the next one if rate limited.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

TMP_DIR = str(PROJECT_ROOT / ".tmp")
OUTPUT_DIR = str(PROJECT_ROOT / "output")
CONFIG_DIR = str(PROJECT_ROOT / "config")

# Paths used throughout
RAW_JOBS = os.path.join(TMP_DIR, "raw_jobs.json")
PARSED_JOBS = os.path.join(TMP_DIR, "parsed_jobs.json")
SCORED_JOBS = os.path.join(TMP_DIR, "scored_jobs.json")
SELECTED_JOBS = os.path.join(TMP_DIR, "selected_jobs.json")
ANALYZED_JOBS = os.path.join(TMP_DIR, "analyzed_jobs.json")
SEARCH_FILTERS = os.path.join(CONFIG_DIR, "search_filters.yaml")
USER_PROFILE = os.path.join(CONFIG_DIR, "user_profile.yaml")
REPORT_PATH = os.path.join(OUTPUT_DIR, "summary_report.md")

MAX_ITERATIONS = 30
MAX_CONTEXT_CHARS = 12000


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Tracks pipeline progress across the agent loop."""
    jobs_scraped: int = 0
    jobs_parsed: int = 0
    jobs_scored: int = 0
    jobs_selected: int = 0
    jobs_analyzed: int = 0
    jobs_generated: int = 0
    report_generated: bool = False
    search_config_path: str = SEARCH_FILTERS
    profile_path: str = USER_PROFILE
    resume_path: str | None = None
    has_profile: bool = False
    errors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool wrappers — each returns a string observation for the agent
# ---------------------------------------------------------------------------

def tool_scrape_jobs(state: AgentState, params: dict) -> str:
    from scrape_jobs import scrape_jobs
    method = params.get("method", "auto")
    try:
        jobs = scrape_jobs(state.search_config_path, method, RAW_JOBS)
        state.jobs_scraped = len(jobs) if jobs else 0
        if not jobs:
            return "Scrape completed but returned 0 jobs. The API may be rate-limiting or the search is too narrow."
        return f"Scraped {len(jobs):,} jobs and saved to .tmp/raw_jobs.json."
    except Exception as e:
        state.errors.append(str(e))
        return f"Scrape failed: {e}"


def tool_parse_jobs(state: AgentState, params: dict) -> str:
    from parse_jobs import parse_jobs
    with open(state.search_config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    try:
        parsed = parse_jobs(RAW_JOBS, PARSED_JOBS, config=config)
        state.jobs_parsed = len(parsed) if parsed else 0
        if not parsed:
            return "Parsing complete but 0 jobs remained after filtering. Filters may be too strict."
        return f"Parsed and filtered: {len(parsed):,} jobs remain (from {state.jobs_scraped:,} raw)."
    except Exception as e:
        state.errors.append(str(e))
        return f"Parse failed: {e}"


def tool_score_jobs(state: AgentState, params: dict) -> str:
    if not state.has_profile:
        return "Cannot score — no user profile loaded. Use resume-only mode or create a profile."
    from score_jobs import score_jobs
    try:
        scored = score_jobs(PARSED_JOBS, state.profile_path, SCORED_JOBS)
        state.jobs_scored = len(scored) if scored else 0
        if not scored:
            return "Scoring complete but produced no results."
        # Build a summary of the top matches
        top = scored[:10]
        lines = [f"Scored {len(scored):,} jobs. Top matches:"]
        for i, j in enumerate(top):
            score = j.get("match_score", 0)
            lines.append(f"  {i+1}. [{score:.0f}%] {j.get('title', '?')} at {j.get('company', '?')}")
        # Distribution
        above_70 = sum(1 for j in scored if j.get("match_score", 0) >= 70)
        above_50 = sum(1 for j in scored if j.get("match_score", 0) >= 50)
        above_35 = sum(1 for j in scored if j.get("match_score", 0) >= 35)
        lines.append(f"\nDistribution: {above_70} jobs ≥70%, {above_50} ≥50%, {above_35} ≥35%")
        return "\n".join(lines)
    except Exception as e:
        state.errors.append(str(e))
        return f"Scoring failed: {e}"


def tool_analyze_jobs(state: AgentState, params: dict) -> str:
    from analyze_jobs import analyze_jobs
    input_path = params.get("input_path", SELECTED_JOBS)
    batch_size = params.get("batch_size", 3)
    try:
        analyzed = analyze_jobs(input_path, ANALYZED_JOBS, batch_size)
        state.jobs_analyzed = len(analyzed) if analyzed else 0
        return f"Analyzed {len(analyzed):,} jobs with LLM. Enriched data saved to .tmp/analyzed_jobs.json."
    except Exception as e:
        state.errors.append(str(e))
        return f"Analysis failed: {e}"


def tool_generate_documents(state: AgentState, params: dict) -> str:
    from generate_documents import generate_documents
    input_path = params.get("input_path", ANALYZED_JOBS)
    max_jobs = params.get("max_jobs", state.jobs_selected or 5)
    try:
        count = generate_documents(
            jobs_path=input_path,
            profile_path=state.profile_path if state.has_profile else None,
            base_resume_path=state.resume_path,
            output_dir=OUTPUT_DIR,
            threshold=0,
            max_jobs=max_jobs,
        )
        state.jobs_generated = count
        return f"Generated application documents for {count} job(s). Files in output/applications/."
    except Exception as e:
        state.errors.append(str(e))
        return f"Document generation failed: {e}"


def tool_generate_report(state: AgentState, params: dict) -> str:
    if not state.has_profile:
        return "Cannot generate report — no user profile for skill gap analysis."
    from generate_report import generate_report
    try:
        generate_report(
            jobs_path=SCORED_JOBS,
            config_path=state.search_config_path,
            profile_path=state.profile_path,
            output_path=REPORT_PATH,
        )
        state.report_generated = True
        return "Summary report generated at output/summary_report.md."
    except Exception as e:
        state.errors.append(str(e))
        return f"Report generation failed: {e}"


def tool_read_file(state: AgentState, params: dict) -> str:
    path = params.get("path", "")
    if not path:
        return "Error: 'path' parameter is required."
    # Security: only allow reading within project
    resolved = os.path.abspath(path)
    if not resolved.startswith(str(PROJECT_ROOT)):
        return "Error: Can only read files within the project directory."
    if not os.path.exists(resolved):
        return f"File not found: {path}"
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
        # Truncate very long files
        if len(content) > 4000:
            content = content[:4000] + "\n... [truncated, file is longer]"
        return content
    except Exception as e:
        return f"Error reading file: {e}"


def tool_evaluate_document(state: AgentState, params: dict) -> str:
    """LLM critiques a generated document and returns score + recommendation."""
    from llm_client import chat_completion

    doc_path = params.get("path", "")
    doc_type = params.get("type", "cover letter")  # "cover letter" or "resume"
    if not doc_path or not os.path.exists(doc_path):
        return f"Document not found: {doc_path}"

    try:
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading document: {e}"

    if len(content) > 3000:
        content = content[:3000]

    system = (
        f"You are a hiring manager reviewing a {doc_type}. "
        "Score it 1-10 and give a brief assessment. Format:\n"
        "Score: X/10\n"
        "Strengths: ...\n"
        "Weaknesses: ...\n"
        "Recommendation: KEEP | REVISE | REWRITE"
    )
    try:
        result = chat_completion(system, content, max_tokens=500)
        return f"Evaluation of {doc_path}:\n{result}"
    except Exception as e:
        return f"Evaluation failed: {e}"


def tool_read_search_filters(state: AgentState, params: dict) -> str:
    if not os.path.exists(state.search_config_path):
        return "Search filters file not found."
    try:
        with open(state.search_config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return f"Current search filters:\n{yaml.dump(config, default_flow_style=False)}"
    except Exception as e:
        return f"Error reading search filters: {e}"


def tool_propose_filter_changes(state: AgentState, params: dict) -> str:
    changes = params.get("changes", {})
    reason = params.get("reason", "No reason given.")
    if not changes:
        return "No changes proposed."

    # Show what would change
    try:
        with open(state.search_config_path, "r", encoding="utf-8") as f:
            current = yaml.safe_load(f)
    except Exception:
        return "Could not read current filters."

    lines = [f"Proposed filter changes (reason: {reason}):"]
    for key, value in changes.items():
        old = current.get(key, "not set")
        lines.append(f"  {key}: {old} → {value}")

    # User must approve
    print("\n" + "\n".join(lines))
    confirm = input("\nApply these changes? (y/n) ").strip().lower()
    if confirm != "y":
        return "User rejected the proposed filter changes."

    # Apply
    current.update(changes)
    with open(state.search_config_path, "w", encoding="utf-8") as f:
        yaml.dump(current, f, default_flow_style=False)
    return "Filter changes applied. You should re-scrape and re-score with the new filters."


def tool_list_applications(state: AgentState, params: dict) -> str:
    apps_dir = os.path.join(OUTPUT_DIR, "applications")
    if not os.path.exists(apps_dir):
        return "No applications directory found."
    entries = os.listdir(apps_dir)
    if not entries:
        return "Applications directory is empty."
    lines = ["Generated applications:"]
    for entry in sorted(entries):
        full = os.path.join(apps_dir, entry)
        if os.path.isdir(full):
            files = os.listdir(full)
            lines.append(f"  {entry}/  ({', '.join(files)})")
    return "\n".join(lines) if len(lines) > 1 else "No application folders found."


def tool_select_jobs(state: AgentState, params: dict) -> str:
    """Select top N jobs from scored results for application generation."""
    count = params.get("count", 5)
    threshold = params.get("threshold", 35.0)

    if not os.path.exists(SCORED_JOBS):
        return "No scored jobs found. Run scoring first."

    with open(SCORED_JOBS, "r", encoding="utf-8") as f:
        scored = json.load(f)

    qualifying = [j for j in scored if j.get("match_score", 0) >= threshold]
    selected = qualifying[:count]
    state.jobs_selected = len(selected)

    if not selected:
        return f"No jobs scoring ≥{threshold}%. Try lowering the threshold."

    os.makedirs(TMP_DIR, exist_ok=True)
    with open(SELECTED_JOBS, "w", encoding="utf-8") as f:
        json.dump(selected, f, indent=2, ensure_ascii=False)

    lines = [f"Selected {len(selected)} job(s) for application generation:"]
    for i, j in enumerate(selected):
        score = j.get("match_score", 0)
        lines.append(f"  {i+1}. [{score:.0f}%] {j.get('title', '?')} at {j.get('company', '?')}")
    return "\n".join(lines)


def tool_check_in(state: AgentState, params: dict) -> str:
    """Pause and ask the user a question via CLI."""
    message = params.get("message", "How would you like to proceed?")
    options = params.get("options", [])

    print(f"\n{'─' * 50}")
    print(f"  Agent check-in: {message}")
    if options:
        for i, opt in enumerate(options):
            print(f"    {i+1}. {opt}")
        print(f"{'─' * 50}")
        choice = input("  Your choice (number or text): ").strip()
    else:
        print(f"{'─' * 50}")
        choice = input("  Your response: ").strip()

    return f"User responded: {choice}"


def tool_finish(state: AgentState, params: dict) -> str:
    summary = params.get("summary", "Pipeline complete.")
    return f"FINISH: {summary}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {
    "scrape_jobs": {
        "fn": tool_scrape_jobs,
        "description": "Scrape job listings from hiring.cafe. Takes optional 'method' (auto/browser/api).",
        "needs_approval": True,
    },
    "parse_jobs": {
        "fn": tool_parse_jobs,
        "description": "Parse, normalize, deduplicate, and filter raw scraped jobs against search criteria.",
        "needs_approval": False,
    },
    "score_jobs": {
        "fn": tool_score_jobs,
        "description": "Score filtered jobs against user profile (skills, experience, education). No LLM needed.",
        "needs_approval": False,
    },
    "select_jobs": {
        "fn": tool_select_jobs,
        "description": "Select top N jobs from scored results. Takes 'count' (int) and optional 'threshold' (float, default 35).",
        "needs_approval": True,
    },
    "analyze_jobs": {
        "fn": tool_analyze_jobs,
        "description": "LLM-enrich selected jobs with deeper analysis (role summary, red flags, culture). Uses LLM credits.",
        "needs_approval": True,
    },
    "generate_documents": {
        "fn": tool_generate_documents,
        "description": "Generate tailored resume + cover letter PDFs for selected jobs. Uses LLM credits.",
        "needs_approval": True,
    },
    "generate_report": {
        "fn": tool_generate_report,
        "description": "Generate summary report with rankings and skill gaps at output/summary_report.md.",
        "needs_approval": False,
    },
    "read_file": {
        "fn": tool_read_file,
        "description": "Read a file's contents. Takes 'path' (relative or absolute within project).",
        "needs_approval": False,
    },
    "evaluate_document": {
        "fn": tool_evaluate_document,
        "description": "LLM critiques a generated document (resume/cover letter). Takes 'path' and 'type'. Returns score and KEEP/REVISE/REWRITE.",
        "needs_approval": False,
    },
    "read_search_filters": {
        "fn": tool_read_search_filters,
        "description": "Read the current search filters YAML config.",
        "needs_approval": False,
    },
    "propose_filter_changes": {
        "fn": tool_propose_filter_changes,
        "description": "Propose changes to search filters. Takes 'changes' (dict of key:value) and 'reason'. User must approve.",
        "needs_approval": True,
    },
    "list_applications": {
        "fn": tool_list_applications,
        "description": "List all generated application folders and their files.",
        "needs_approval": False,
    },
    "check_in": {
        "fn": tool_check_in,
        "description": "Pause and ask the user a question. Takes 'message' (str) and optional 'options' (list of strings).",
        "needs_approval": False,
    },
    "finish": {
        "fn": tool_finish,
        "description": "End the agent loop. Takes 'summary' (str) with a final summary for the user.",
        "needs_approval": False,
    },
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(state: AgentState) -> str:
    tool_descriptions = "\n".join(
        f"  - {name}: {info['description']}"
        + (" [REQUIRES USER APPROVAL]" if info["needs_approval"] else "")
        for name, info in TOOLS.items()
    )

    return f"""You are the orchestrator of an Agentic Job Search Pipeline. You reason about what to do, call tools, observe results, and adapt.

## Available Tools
{tool_descriptions}

## Response Format
You MUST respond in exactly this format every turn:

Thought: <your reasoning about what to do next, 1-3 sentences>
Action: <tool_name>
Action Input: <JSON object with parameters, or {{}} if no parameters>

## Rules
1. ALWAYS follow the Thought/Action/Action Input format. Never skip any field.
2. Call exactly ONE tool per turn.
3. The typical pipeline order is: scrape_jobs → parse_jobs → score_jobs → select_jobs → analyze_jobs → generate_documents → generate_report → finish
4. You CAN deviate from this order when it makes sense (e.g., re-scrape after filter changes, skip analysis, evaluate documents after generation).
5. MANDATORY CHECK-INS — you MUST use check_in:
   - After scoring: show the user top matches and ask how many to generate for
   - Before any tool marked [REQUIRES USER APPROVAL]: confirm the action
   - If you detect poor results (e.g., 0 jobs above 50%): propose filter changes
   - If an error is unrecoverable: ask the user how to proceed
6. SELF-EVALUATION — after generate_documents completes:
   - Use read_file to read at least one cover_letter.md
   - Use evaluate_document to get a quality score
   - If the recommendation is REWRITE, regenerate that job's documents
7. ADAPTIVE SEARCH — if scoring shows poor results:
   - Use read_search_filters to check current config
   - Propose specific filter changes via check_in (explain your reasoning)
   - If user approves, use propose_filter_changes then re-scrape and re-score
8. Always call finish when you're done, with a summary of what was accomplished.

## Current State
- Jobs scraped: {state.jobs_scraped}
- Jobs parsed: {state.jobs_parsed}
- Jobs scored: {state.jobs_scored}
- Jobs selected: {state.jobs_selected}
- Jobs analyzed: {state.jobs_analyzed}
- Documents generated: {state.jobs_generated}
- Report generated: {state.report_generated}
- Has user profile: {state.has_profile}
- Resume provided: {state.resume_path is not None}
- Errors so far: {len(state.errors)}
"""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_response(text: str) -> tuple[str, str, dict]:
    """Extract Thought, Action, and Action Input from the LLM response.

    Returns (thought, action, params). Falls back to check_in if parsing fails.
    """
    thought = ""
    action = ""
    params = {}

    # Extract Thought
    thought_match = re.search(r"Thought:\s*(.+?)(?=\nAction:|\Z)", text, re.DOTALL)
    if thought_match:
        thought = thought_match.group(1).strip()

    # Extract Action
    action_match = re.search(r"Action:\s*(\S+)", text)
    if action_match:
        action = action_match.group(1).strip()

    # Extract Action Input
    input_match = re.search(r"Action Input:\s*(.+)", text, re.DOTALL)
    if input_match:
        raw_input = input_match.group(1).strip()
        try:
            params = json.loads(raw_input)
        except json.JSONDecodeError:
            # Try to find JSON object within the text
            json_match = re.search(r"\{.*\}", raw_input, re.DOTALL)
            if json_match:
                try:
                    params = json.loads(json_match.group())
                except json.JSONDecodeError:
                    params = {}

    # Fallback: if we couldn't parse an action, treat as check_in
    if not action or action not in TOOLS:
        return (
            thought or text[:200],
            "check_in",
            {"message": f"I couldn't determine my next action. My response was: {text[:300]}"},
        )

    return thought, action, params


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------

def truncate_messages(messages: list[dict], max_chars: int = MAX_CONTEXT_CHARS) -> list[dict]:
    """Keep messages within a character budget. Preserves system prompt + recent messages."""
    if not messages:
        return messages

    total = sum(len(m.get("content", "")) for m in messages)
    if total <= max_chars:
        return messages

    # Always keep the system message (first) and the last 6 messages
    system = messages[0] if messages[0]["role"] == "system" else None
    keep_recent = 6
    recent = messages[-keep_recent:]

    if system:
        trimmed = [system]
        trimmed.append({
            "role": "user",
            "content": "[Earlier conversation was trimmed to save context. The agent has been working through the pipeline steps. Recent history follows.]"
        })
        trimmed.extend(recent)
        return trimmed
    return recent


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_agent(
    state: AgentState,
    skip_scrape: bool = False,
    force_scrape: bool = False,
):
    """Run the ReAct agent loop."""
    from llm_client import chat_completion_multi, get_call_delay, provider_status

    print("=" * 60)
    print("  Agentic Job Search Pipeline — Agent Mode")
    print("=" * 60)
    print(provider_status())
    print(f"  Profile:     {'loaded' if state.has_profile else 'none'}")
    if state.resume_path:
        print(f"  Resume:      {state.resume_path}")
    print()

    # Build initial context
    system_prompt = build_system_prompt(state)
    initial_context = "Begin the pipeline. "
    if skip_scrape:
        if os.path.exists(RAW_JOBS):
            with open(RAW_JOBS, "r", encoding="utf-8") as f:
                raw_count = len(json.load(f))
            state.jobs_scraped = raw_count
            initial_context += f"Scraping is skipped — using cached raw_jobs.json ({raw_count:,} jobs). Start with parse_jobs."
        else:
            initial_context += "Scraping is skipped but no cached data exists. Ask the user what to do."
    elif force_scrape:
        initial_context += "The user requested a fresh scrape. Start with scrape_jobs."
    else:
        # Check cache
        from run_pipeline import _is_cache_valid
        if _is_cache_valid(RAW_JOBS, state.search_config_path):
            with open(RAW_JOBS, "r", encoding="utf-8") as f:
                raw_count = len(json.load(f))
            state.jobs_scraped = raw_count
            initial_context += f"Cached scrape data found ({raw_count:,} jobs, less than 24h old). You can skip scraping or re-scrape if needed."
        else:
            initial_context += "No valid cached data. Start with scrape_jobs."

    if not state.has_profile and not state.resume_path:
        initial_context += " WARNING: No user profile or resume found — scoring will not work."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_context},
    ]

    for iteration in range(MAX_ITERATIONS):
        # Truncate context if needed
        messages = truncate_messages(messages)

        # Update system prompt with latest state
        messages[0]["content"] = build_system_prompt(state)

        # REASON — call the reasoning LLM
        try:
            response = chat_completion_multi(
                messages,
                max_tokens=1024,
            )
        except Exception as e:
            print(f"\n  Agent reasoning error: {e}")
            print("  Falling back to check-in...")
            response = "Thought: Reasoning LLM failed.\nAction: check_in\nAction Input: {\"message\": \"My reasoning LLM hit an error. How should I proceed?\"}"

        # Parse response
        thought, action, params = parse_response(response)

        # Display thought
        print(f"\n{'─' * 50}")
        print(f"  Step {iteration + 1}: {thought[:120]}")
        print(f"  → {action}({json.dumps(params)[:80]})")

        # Check for finish
        if action == "finish":
            observation = tool_finish(state, params)
            print(f"\n{'=' * 60}")
            print(f"  {observation}")
            print(f"{'=' * 60}")
            break

        # Check approval for tools that need it
        tool_info = TOOLS.get(action)
        if not tool_info:
            observation = f"Unknown tool: {action}. Available tools: {', '.join(TOOLS.keys())}"
        elif tool_info["needs_approval"]:
            print(f"\n  This action requires your approval.")
            confirm = input(f"  Proceed with {action}? (y/n) ").strip().lower()
            if confirm != "y":
                observation = f"User declined to run {action}."
            else:
                observation = tool_info["fn"](state, params)
        else:
            observation = tool_info["fn"](state, params)

        # Truncate very long observations
        if len(observation) > 3000:
            observation = observation[:3000] + "\n... [truncated]"

        print(f"  Result: {observation[:150]}")

        # OBSERVE — append to conversation
        messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

        # Rate limit delay for reasoning LLM
        delay = get_call_delay()
        if delay > 0:
            time.sleep(delay)

    else:
        print(f"\n  Agent hit maximum iterations ({MAX_ITERATIONS}). Stopping.")

    # Print final state summary
    print(f"\n  Final state:")
    print(f"    Scraped: {state.jobs_scraped} | Parsed: {state.jobs_parsed} | Scored: {state.jobs_scored}")
    print(f"    Selected: {state.jobs_selected} | Analyzed: {state.jobs_analyzed} | Generated: {state.jobs_generated}")
    print(f"    Report: {'yes' if state.report_generated else 'no'}")
    if state.errors:
        print(f"    Errors: {len(state.errors)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="ReAct agent for the job search pipeline")
    parser.add_argument("--resume", default=None,
                        help="Path to base resume file")
    parser.add_argument("--skip-scrape", action="store_true",
                        help="Use cached raw_jobs.json")
    parser.add_argument("--force-scrape", action="store_true",
                        help="Force re-scrape even if cache is valid")
    args = parser.parse_args()

    state = AgentState(
        resume_path=args.resume,
        has_profile=os.path.exists(USER_PROFILE),
    )

    run_agent(state, skip_scrape=args.skip_scrape, force_scrape=args.force_scrape)


if __name__ == "__main__":
    main()
