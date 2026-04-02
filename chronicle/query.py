"""Search and browse the Decision Chronicle.

Usage:
    chronicle query sessions                 # current project's chronicles
    chronicle query projects                 # all chronicled projects
    chronicle query timeline                 # recent sessions across all projects
    chronicle query search "auth"            # full-text search
"""

import argparse
import os
import re
from pathlib import Path

from .config import PROJECTS_DIR


def search(query: str, project: str | None = None):
    """Full-text search across all chronicle markdown files."""
    if not PROJECTS_DIR.exists():
        print("No chronicles found. Run the batch processor or daemon first.")
        return

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for md_file in sorted(PROJECTS_DIR.rglob("*.md")):
        if project and project not in str(md_file):
            continue

        content = md_file.read_text()
        matches = list(pattern.finditer(content))
        if not matches:
            continue

        # Extract context around each match
        for match in matches:
            start = max(0, match.start() - 100)
            end = min(len(content), match.end() + 100)
            context = content[start:end].replace("\n", " ").strip()
            # Highlight match
            context = context.replace(match.group(), f"**{match.group()}**")
            results.append((md_file, context))

    if not results:
        print(f"No results for '{query}'")
        return

    print(f"Found {len(results)} match(es) for '{query}':\n")
    current_file = None
    for filepath, context in results:
        if filepath != current_file:
            rel = filepath.relative_to(PROJECTS_DIR)
            print(f"--- {rel} ---")
            current_file = filepath
        print(f"  ...{context}...")
        print()


def timeline(limit: int = 20, project: str | None = None):
    """Show recent session records, newest first."""
    if not PROJECTS_DIR.exists():
        print("No chronicles found.")
        return

    sessions = []
    for session_file in PROJECTS_DIR.rglob("sessions/*.md"):
        if project and project not in str(session_file):
            continue
        content = session_file.read_text()
        # Extract date — handles both "**Date**: X" and "**Date**: X |" formats
        date_match = re.search(r"\*\*Date\*\*:\s*([^|\n]+)", content)
        date_str = date_match.group(1).strip() if date_match else "0000"
        # Extract title — handles both "# Session: X" and "# <title>" formats
        title_match = re.search(r"^# (.+)", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else session_file.stem[:8]
        project_slug = session_file.parent.parent.name
        sessions.append((date_str, project_slug, title, session_file, content))

    sessions.sort(key=lambda x: x[0], reverse=True)

    if not sessions:
        print("No session records found.")
        return

    print(f"Recent sessions (showing {min(limit, len(sessions))} of {len(sessions)}):\n")
    for date_str, proj, title, filepath, content in sessions[:limit]:
        # Extract decisions count (### headings under Key decisions)
        decision_count = len(re.findall(r"^### ", content, re.MULTILINE))
        # Extract summary — matches both "## Summary" section and inline summary
        summary_match = re.search(r"## Summary\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        if not summary_match:
            summary_match = re.search(r"## What happened\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        summary = summary_match.group(1).strip()[:150] if summary_match else ""

        print(f"  [{date_str}] {proj}")
        print(f"    Session {title}")
        if summary:
            print(f"    {summary}")
        if decision_count:
            print(f"    ({decision_count} decisions)")
        print()


def sessions(project_path: str | None = None):
    """Show the chronicle for the current project (or a given path)."""
    cwd = project_path or os.environ.get("CHRONICLE_ORIGINAL_CWD", os.getcwd())
    slug = cwd.replace("/", "-")
    project_dir = PROJECTS_DIR / slug
    chronicle_file = project_dir / "chronicle.md"
    sessions_dir = project_dir / "sessions"

    if not project_dir.exists():
        # Check if sessions exist but haven't been processed yet
        claude_sessions = Path.home() / ".claude" / "projects" / slug
        if claude_sessions.exists():
            jsonl_count = len(list(claude_sessions.glob("*.jsonl")))
            if jsonl_count:
                from .daemon import _is_running
                running, pid = _is_running()
                if running:
                    print(f"Not yet processed. {jsonl_count} session(s) pending in {cwd}")
                    print(f"Daemon is running (pid {pid}) — will process after 5 minutes of inactivity.")
                    print(f"\nTo process now:")
                    print(f"  chronicle batch --project {slug.split('-')[-1]} --workers 5")
                else:
                    print(f"Not yet processed. {jsonl_count} session(s) found for {cwd}")
                    print(f"Daemon is not running. Process manually:")
                    print(f"  chronicle batch --project {slug.split('-')[-1]} --workers 5")
                return
        print(f"No sessions found for {cwd}")
        return

    session_count = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0

    if chronicle_file.exists():
        print(f"Chronicle for {cwd} ({session_count} sessions):\n")
        print(f"  vim {chronicle_file}")
        print()
        if sessions_dir.exists():
            print(f"Detailed per-session files:")
            for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
                with open(md_file, errors="ignore") as f:
                    first_line = f.readline().rstrip("\n")
                title = first_line[2:] if first_line.startswith("# ") else md_file.stem
                print(f"  {title}")
                print(f"    vim {md_file}")
            print()
    elif session_count > 0:
        print(f"Chronicles for {cwd} ({session_count} sessions):\n")
        for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
            with open(md_file, errors="ignore") as f:
                first_line = f.readline().rstrip("\n")
            title = first_line[2:] if first_line.startswith("# ") else md_file.stem
            print(f"  {title}")
            print(f"    vim {md_file}")
        print()
    else:
        print(f"No chronicles for {cwd}")


def show_project(name: str):
    """Show chronicle for a project by name (partial match on slug)."""
    if not PROJECTS_DIR.exists():
        print(f"No chronicles found for '{name}'.")
        return

    # Find matching project directories (partial match)
    matches = []
    for project_dir in sorted(PROJECTS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        if name in project_dir.name:
            matches.append(project_dir)

    if not matches:
        # Check unprocessed sessions in ~/.claude/projects/
        claude_projects = Path.home() / ".claude" / "projects"
        if claude_projects.exists():
            pending = [d for d in claude_projects.iterdir()
                       if d.is_dir() and name in d.name
                       and list(d.glob("*.jsonl"))]
            if pending:
                for d in pending:
                    count = len(list(d.glob("*.jsonl")))
                    print(f"  {d.name}: {count} session(s) not yet processed")
                print(f"\nProcess with: chronicle batch --workers 5")
                return
        print(f"No chronicles found matching '{name}'.")
        return

    for project_dir in matches:
        sessions_dir = project_dir / "sessions"
        chronicle_file = project_dir / "chronicle.md"
        session_count = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0

        print(f"Project: {project_dir.name} ({session_count} sessions)")
        if chronicle_file.exists():
            print(f"  Chronicle: vim {chronicle_file}\n")

        if sessions_dir.exists():
            for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
                with open(md_file, errors="ignore") as f:
                    first_line = f.readline().rstrip("\n")
                title = first_line[2:] if first_line.startswith("# ") else md_file.stem
                print(f"  {title}")
                print(f"    vim {md_file}")
            print()


def list_projects():
    """List all chronicled projects. If none, show available projects from Claude Code."""
    # Show chronicled projects
    has_chronicles = False
    if PROJECTS_DIR.exists():
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            sessions_dir = project_dir / "sessions"
            count = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0
            if count:
                has_chronicles = True
                print(f"  {project_dir.name}: {count} sessions")

    if has_chronicles:
        return

    # No chronicles — show what's available in ~/.claude/projects/
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        print("No chronicles and no sessions found.")
        print("Start a coding session first, then run: chronicle batch --workers 5")
        return

    projects = {}
    for project_dir in sorted(claude_projects.iterdir()):
        if not project_dir.is_dir():
            continue
        count = len([f for f in project_dir.glob("*.jsonl") if "subagent" not in str(f)])
        if count:
            name = project_dir.name.split("-")[-1] or project_dir.name
            projects[name] = (count, project_dir.name)

    if not projects:
        print("No sessions found.")
        return

    total_sessions = sum(c for c, _ in projects.values())
    print(f"No chronicles yet. Found {len(projects)} projects with {total_sessions} sessions:\n")
    for name, (count, slug) in sorted(projects.items()):
        print(f"  {slug}: {count} sessions")
    print(f"\nProcess all:   chronicle batch --workers 5")
    print(f"Process one:   chronicle batch --project <folder-name> --workers 5")
    print(f"Preview:       chronicle batch --dry-run")


def main():
    parser = argparse.ArgumentParser(description="Query the Decision Chronicle")
    subparsers = parser.add_subparsers(dest="command")

    search_p = subparsers.add_parser("search", help="Full-text search")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--project", help="Filter to project")

    timeline_p = subparsers.add_parser("timeline", help="Recent decisions")
    timeline_p.add_argument("--limit", type=int, default=20)
    timeline_p.add_argument("--project", help="Filter to project")

    sessions_p = subparsers.add_parser("sessions", help="Sessions for current project")
    sessions_p.add_argument("path", nargs="?", help="Project path (default: current dir)")

    subparsers.add_parser("projects", help="List chronicled projects")

    # If the first arg isn't a known subcommand, treat it as a project name
    # e.g. `chronicle query medium` → show that project's sessions/timeline
    import sys
    known = {"search", "timeline", "sessions", "projects", "-h", "--help"}
    if len(sys.argv) > 1 and sys.argv[1] not in known:
        project_name = sys.argv[1]
        show_project(project_name)
        return

    args = parser.parse_args()

    if args.command == "search":
        search(args.query, args.project)
    elif args.command == "timeline":
        timeline(args.limit, getattr(args, "project", None))
    elif args.command == "sessions":
        sessions(getattr(args, "path", None))
    elif args.command == "projects":
        list_projects()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
