"""Generate a unified project story from all chronicle sessions.

Reads every session markdown file chronologically, sends the full content
to Claude for synthesis into a single cohesive narrative. The output is a
stakeholder-readable markdown document covering the entire project arc:
architecture, decisions, problems solved, evolution over time.

Usage:
    chronicle story [project]     # project name (substring match)
    chronicle story               # current directory's project
"""

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

from .claude_cli import spawn_claude
from .config import projects_dir, load_config


def _find_project(name: str | None) -> Path | None:
    if not projects_dir().exists():
        return None
    if name:
        matches = [d for d in sorted(projects_dir().iterdir())
                    if d.is_dir() and name in d.name]
        return matches[0] if matches else None
    cwd = os.getcwd()
    slug = cwd.replace("/", "-")
    project_dir = projects_dir() / slug
    return project_dir if project_dir.exists() else None


def _load_session_content(project_dir: Path) -> list[tuple[str, str]]:
    """Load all session markdown files as (filename, content) chronologically."""
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return []
    pairs = []
    for md_file in sorted(sessions_dir.glob("*.md")):
        content = md_file.read_text(errors="ignore")
        # Strip the turn-by-turn log and verbatim prompts — too noisy for synthesis
        content = re.sub(
            r"## Turn-by-turn log\n\n```.*?```\n\n",
            "", content, flags=re.DOTALL
        )
        content = re.sub(
            r"---\n\n<details><summary>User prompts \(verbatim\)</summary>.*?</details>\n",
            "", content, flags=re.DOTALL
        )
        pairs.append((md_file.name, content))
    return pairs


STORY_PROMPT = """\
You are writing a unified engineering story for a software project. You will receive \
the full content of every chronicle session in chronological order. Your job is to \
synthesize them into a single, cohesive markdown document that a technical lead or \
CTO can read to understand the entire project: what was built, how it evolved, every \
significant decision, every problem encountered and solved, the architecture, and \
where things stand now.

RULES:
- Write in chronological order. The reader should feel the project unfolding.
- Preserve exact technical details: filenames, commands, config values, error messages, \
numbers, versions, timings. Do not generalize.
- Preserve ALL decisions with their rationale and alternatives considered.
- Preserve ALL problems solved with diagnosis and verification.
- Group related work into logical phases or chapters, not by session. A phase might \
span multiple sessions or a single session might contain multiple phases.
- Include the developer's reasoning and judgment calls — what they were optimizing for, \
where they pushed back, what changed their mind.
- Include tool usage patterns: which Claude Code tools were used, what commands were run, \
what files were read or modified. This shows the engineering workflow.
- End with a "Current State" section: what's working, what's not, open questions, \
next steps.
- Write like a senior engineer's project journal, not a formal report. Use first \
person plural ("we decided", "we hit a wall", "turns out the issue was").
- Use markdown headings, bullet lists, and code blocks for readability.
- Start with a brief project overview paragraph, then dive into the chronological story.
- Do NOT omit sessions even if they seem trivial — include them as brief notes to \
maintain the complete timeline.

Return ONLY the markdown document. No fences wrapping it. Start with a # heading.

CHRONICLE SESSIONS (chronological):

{sessions}
"""


def generate_story(project_name: str | None = None):
    """Generate a unified project story from all chronicle sessions."""
    project_dir = _find_project(project_name)
    if not project_dir:
        if project_name:
            print(f"No chronicles found for '{project_name}'.")
        else:
            print("No chronicles found for current directory.")
        print("Run: chronicle query projects")
        return

    session_pairs = _load_session_content(project_dir)
    if not session_pairs:
        print(f"No sessions in {project_dir.name}")
        return

    # Build the session content block
    session_blocks = []
    for filename, content in session_pairs:
        session_blocks.append(f"=== SESSION: {filename} ===\n{content}")
    sessions_text = "\n\n".join(session_blocks)

    # Truncate if enormous — keep head (early sessions) + tail (recent sessions)
    # so the "Current State" section has fresh data to work with
    max_chars = 400_000
    if len(sessions_text) > max_chars:
        half = max_chars // 2
        sessions_text = (sessions_text[:half]
                         + "\n\n[... middle sessions truncated ...]\n\n"
                         + sessions_text[-half:])

    prompt = STORY_PROMPT.format(sessions=sessions_text)

    config = load_config()
    model = config.get("model", "opus")
    fallback = config.get("fallback_model", "sonnet")

    print(f"  Generating story for {project_dir.name} "
          f"({len(session_pairs)} sessions)...")

    async def _generate():
        res = await spawn_claude(
            prompt=prompt, model=model, fallback_model=fallback,
            effort="max", timeout=600,
        )
        if not res.ok:
            print(f"  Error ({res.error_kind.value}): {res.error_message[:200]}",
                  file=sys.stderr)
            return None
        if res.total_cost_usd:
            print(f"  Cost: ${res.total_cost_usd:.2f}")
        return (res.stdout_json or {}).get("result", "")

    result = asyncio.run(_generate())
    if not result:
        print("  Failed to generate story.")
        return

    md = result.strip()
    # Strip markdown fences if the model wrapped it
    if md.startswith("```"):
        first_newline = md.index("\n")
        last_fence = md.rfind("```")
        if last_fence > first_newline:
            md = md[first_newline + 1:last_fence].strip()

    output_path = project_dir / "story.md"
    output_path.write_text(md + "\n")
    line_count = md.count("\n") + 1
    print(f"  Written: {output_path} ({line_count} lines)")
    print(f"  vim {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate unified project story from chronicles"
    )
    parser.add_argument("project", nargs="?",
                        help="Project name (substring match)")
    args = parser.parse_args()
    generate_story(args.project)


if __name__ == "__main__":
    main()
