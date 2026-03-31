"""Per-project insight from chronicle session data.

Generates an LLM-synthesized HTML report via `claude -p`. Aggregates session
data in Python, sends it to Claude for narrative analysis and HTML generation,
writes the result to ~/.chronicle/projects/<slug>/insight.html and opens it.

Usage:
    chronicle insight [project]     # project name (substring match)
    chronicle insight               # current directory's project
"""

import argparse
import asyncio
import json
import os
import re
import sys
import webbrowser
from collections import Counter
from pathlib import Path

from .config import PROJECTS_DIR, load_config


def _find_project(name: str | None) -> Path | None:
    """Resolve project directory from name or cwd."""
    if not PROJECTS_DIR.exists():
        return None

    if name:
        matches = [d for d in sorted(PROJECTS_DIR.iterdir())
                    if d.is_dir() and name in d.name]
        return matches[0] if matches else None

    cwd = os.getcwd()
    slug = cwd.replace("/", "-")
    project_dir = PROJECTS_DIR / slug
    return project_dir if project_dir.exists() else None


def _parse_sessions(project_dir: Path) -> list[dict]:
    """Parse all session markdown files into structured dicts."""
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return []

    sessions = []
    for md_file in sorted(sessions_dir.glob("*.md")):
        content = md_file.read_text(errors="ignore")
        session = {"file": str(md_file)}

        title_match = re.match(r"^# (.+)", content)
        session["title"] = title_match.group(1) if title_match else md_file.stem

        meta_match = re.search(
            r"\*\*Date\*\*:\s*([^|]+)\|.*\*\*Turns\*\*:\s*(\d+)", content
        )
        session["date"] = meta_match.group(1).strip() if meta_match else ""
        session["turns"] = int(meta_match.group(2)) if meta_match else 0

        cost_match = re.search(r"\*\*Cost\*\*:\s*\$([0-9.]+)", content)
        session["cost"] = float(cost_match.group(1)) if cost_match else 0.0

        decisions_section = re.search(
            r"## Key decisions\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if decisions_section:
            session["decisions"] = re.findall(
                r"^### (.+)", decisions_section.group(1), re.MULTILINE
            )
        else:
            session["decisions"] = []

        oq_section = re.search(
            r"## Open questions\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if oq_section:
            session["open_questions"] = re.findall(
                r"^- (.+)", oq_section.group(1), re.MULTILINE
            )
        else:
            session["open_questions"] = []

        fc_section = re.search(
            r"## Files changed\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL
        )
        if fc_section:
            session["files_changed"] = re.findall(
                r"`([^`]+)`", fc_section.group(1)
            )
        else:
            session["files_changed"] = []

        stack_match = re.search(r"\*\*Stack\*\*:\s*(.+)", content)
        if stack_match:
            session["stack"] = [s.strip() for s in stack_match.group(1).split(",")]
        else:
            session["stack"] = []

        session["problems_count"] = len(re.findall(r"\*\*Problem\*\*:", content))

        # Summaries for narrative context
        summary_match = re.search(
            r"## Summary\n\n(.+?)(?=\n\n## |\Z)", content, re.DOTALL
        )
        session["summary"] = summary_match.group(1).strip()[:500] if summary_match else ""

        sessions.append(session)

    return sessions


def _build_data_payload(project_dir: Path, sessions: list[dict]) -> dict:
    """Aggregate session data into a structured payload for the LLM."""
    slug = project_dir.name
    total_turns = sum(s["turns"] for s in sessions)
    total_cost = sum(s["cost"] for s in sessions)
    total_decisions = sum(len(s["decisions"]) for s in sessions)
    total_problems = sum(s["problems_count"] for s in sessions)

    file_counts = Counter()
    for s in sessions:
        for f in s["files_changed"]:
            file_counts[f] += 1

    stack_counts = Counter()
    for s in sessions:
        for tech in s["stack"]:
            stack_counts[tech] += 1

    all_decisions = []
    for s in sessions:
        for d in s["decisions"]:
            all_decisions.append({"decision": re.sub(r"\s+_\w+_$", "", d),
                                  "session": s["title"], "date": s["date"][:10]})

    all_questions = []
    for s in sessions:
        for q in s["open_questions"]:
            all_questions.append({"question": q, "session": s["title"]})

    return {
        "project_slug": slug,
        "session_count": len(sessions),
        "total_turns": total_turns,
        "total_cost_usd": total_cost,
        "total_decisions": total_decisions,
        "total_problems_solved": total_problems,
        "date_range": {
            "first": sessions[0]["date"][:10] if sessions[0]["date"] else "",
            "last": sessions[-1]["date"][:10] if sessions[-1]["date"] else "",
        },
        "sessions": [
            {
                "title": s["title"],
                "date": s["date"][:10] if s["date"] else "",
                "turns": s["turns"],
                "cost": s["cost"],
                "decisions": len(s["decisions"]),
                "problems": s["problems_count"],
                "summary": s["summary"],
            }
            for s in sessions
        ],
        "all_decisions": all_decisions,
        "open_questions": all_questions,
        "most_changed_files": file_counts.most_common(15),
        "technology_stack": stack_counts.most_common(20),
    }


INSIGHT_PROMPT = """\
You are generating an HTML insight report for a software project's engineering chronicle.

You will receive structured data about all sessions in this project. Generate a \
complete, self-contained HTML file with inline CSS (no external dependencies) that \
presents a rich, visually appealing analysis of the project.

The HTML should include:
1. A header with project name, session count, date range, and key stats
2. An executive summary narrative (2-3 paragraphs) synthesizing what this project \
is about, what the major themes are, and how the work evolved over time
3. A timeline of sessions with visual indicators for complexity (turns) and decisions
4. A decisions section grouping related decisions by theme, not just listing them
5. An open questions section highlighting which ones keep recurring or are highest risk
6. A technology stack visualization
7. A most-changed files section
8. If cost data is available, a cost summary

Style guidelines:
- Use a clean, modern design with a dark sidebar or header
- Use CSS grid or flexbox for layout
- Use color-coded badges for decision counts, problem counts
- Make it look like a professional dashboard, not a plain list
- Use monospace font for file paths and technical terms
- Responsive — should look good at 900px+
- No JavaScript required, pure HTML+CSS

Return ONLY the complete HTML document, starting with <!DOCTYPE html>. No markdown \
fences, no explanation before or after.

PROJECT DATA:
{data}
"""


def generate_insight(project_name: str | None = None):
    """Generate LLM-synthesized HTML insight report."""
    project_dir = _find_project(project_name)
    if not project_dir:
        if project_name:
            print(f"No chronicles found for '{project_name}'.")
        else:
            print("No chronicles found for current directory.")
        print("Run: chronicle query projects")
        return

    sessions = _parse_sessions(project_dir)
    if not sessions:
        print(f"No sessions in {project_dir.name}")
        return

    payload = _build_data_payload(project_dir, sessions)
    prompt = INSIGHT_PROMPT.format(data=json.dumps(payload, indent=2))

    config = load_config()
    model = config.get("model", "opus")
    fallback = config.get("fallback_model", "sonnet")

    print(f"  Generating insight for {project_dir.name} "
          f"({len(sessions)} sessions)...")

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    async def _generate():
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            "--effort", "max",
            "--fallback-model", fallback,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=300
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return None
        if proc.returncode != 0:
            err = stderr.decode()[:200] or stdout.decode()[:200]
            print(f"  Error: {err}", file=sys.stderr)
            return None
        try:
            outer = json.loads(stdout.decode())
            return outer.get("result", "")
        except json.JSONDecodeError:
            return stdout.decode()

    result = asyncio.run(_generate())
    if not result:
        print("  Failed to generate insight.")
        return

    # Extract HTML from result (strip any markdown fences if present)
    html = result.strip()
    if html.startswith("```"):
        first_newline = html.index("\n")
        last_fence = html.rfind("```")
        html = html[first_newline + 1:last_fence].strip()

    # Write HTML file
    output_path = project_dir / "insight.html"
    output_path.write_text(html)
    print(f"  Written: {output_path}")

    # Open in browser
    try:
        webbrowser.open(f"file://{output_path}")
        print(f"  Opened in browser.")
    except Exception:
        print(f"  Open manually: xdg-open {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Per-project chronicle insight")
    parser.add_argument("project", nargs="?", help="Project name (substring match)")
    args = parser.parse_args()
    generate_insight(args.project)


if __name__ == "__main__":
    main()
