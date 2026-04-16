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
import shlex
import sys
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
    """Show the chronicle for the current project (or a given path/name)."""
    cwd = project_path or os.environ.get("CHRONICLE_ORIGINAL_CWD", os.getcwd())
    cwd = cwd.rstrip("/")
    slug = cwd.replace("/", "-")
    project_dir = PROJECTS_DIR / slug

    # If exact slug doesn't match, try substring match (e.g. "codex-opinion"
    # matches "-home-synesis-codex-opinion")
    if not project_dir.exists() and PROJECTS_DIR.exists() and project_path:
        matches = [d for d in sorted(PROJECTS_DIR.iterdir())
                    if d.is_dir() and project_path in d.name]
        if matches:
            project_dir = matches[0]
            slug = project_dir.name
            cwd = project_path

    chronicle_file = project_dir / "chronicle.md"
    sessions_dir = project_dir / "sessions"

    if not project_dir.exists():
        # Check if sessions exist but haven't been processed yet
        claude_projects = Path.home() / ".claude" / "projects"
        # Try exact slug first, then substring
        claude_sessions = claude_projects / slug
        if not claude_sessions.exists() and claude_projects.exists() and project_path:
            matches = [d for d in sorted(claude_projects.iterdir())
                        if d.is_dir() and project_path in d.name]
            if matches:
                claude_sessions = matches[0]
        if claude_sessions.exists():
            jsonl_count = len(list(claude_sessions.glob("*.jsonl")))
            if jsonl_count:
                from .mode import is_background_mode
                from .daemon import _is_running
                running, pid = _is_running()
                bg = is_background_mode()
                print(f"Not yet processed. {jsonl_count} session(s) found for '{cwd}'")
                if bg and running:
                    print(f"Daemon is running (pid {pid}) — will process after "
                          f"5 minutes of inactivity.")
                elif bg and not running:
                    print("Mode=background but daemon is not running. "
                          "Run `chronicle doctor`.")
                else:
                    print("Mode=foreground — summarization only happens on demand.")
                # `batch.find_all_sessions` substring-matches --project
                # against the slugged directory name under ~/.claude/projects/,
                # not against raw filesystem paths. Print the resolved slug so
                # the suggested command actually works when copy-pasted.
                process_filter = claude_sessions.name
                print("\nTo process now:")
                print(f"  chronicle process --project {shlex.quote(process_filter)} --workers 5")
                return
        print(f"No sessions found for '{cwd}'")
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
                print(f"\nProcess with: chronicle process --workers 5")
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
    """List projects with per-session breakdown: processed / pending / failed.

    For each project slug (present under ~/.claude/projects/ or ~/.chronicle/projects/):
      - processed: sessions with a success marker AND a session .md file
      - failed (terminal): sessions in .failed/ with terminal=true
      - pending: jsonl exists but no success marker, no terminal-failure marker
    """
    from .storage import is_succeeded, is_terminal_failure
    claude_projects = Path.home() / ".claude" / "projects"

    # Gather slugs from both sides
    slugs: set[str] = set()
    if PROJECTS_DIR.exists():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir():
                slugs.add(d.name)
    if claude_projects.exists():
        for d in claude_projects.iterdir():
            if d.is_dir():
                slugs.add(d.name)

    if not slugs:
        print("No chronicles and no sessions found.")
        print("Start a coding session first, then run: chronicle process --workers 5")
        return

    totals = {"processed": 0, "pending": 0, "failed": 0}
    rows: list[tuple[str, int, int, int]] = []

    for slug in sorted(slugs):
        processed = 0
        pending = 0
        failed = 0

        cp_slug = claude_projects / slug
        if cp_slug.exists():
            for jsonl in cp_slug.glob("*.jsonl"):
                if "subagents" in str(jsonl):
                    continue
                sid = jsonl.stem
                if is_succeeded(sid):
                    processed += 1
                elif is_terminal_failure(sid):
                    failed += 1
                else:
                    pending += 1

        totals["processed"] += processed
        totals["pending"] += pending
        totals["failed"] += failed
        rows.append((slug, processed, pending, failed))

    print(f"  {'Project':50}  {'OK':>4}  {'Pend':>4}  {'Fail':>4}")
    print(f"  {'─' * 50}  {'─' * 4}  {'─' * 4}  {'─' * 4}")
    for slug, p, pe, f in rows:
        if p + pe + f == 0:
            continue
        short = slug if len(slug) <= 50 else slug[:47] + "..."
        print(f"  {short:50}  {p:>4}  {pe:>4}  {f:>4}")
    print(f"  {'─' * 50}  {'─' * 4}  {'─' * 4}  {'─' * 4}")
    print(f"  {'Total':50}  {totals['processed']:>4}  "
          f"{totals['pending']:>4}  {totals['failed']:>4}")

    if totals["pending"]:
        print(f"\n  Process pending:    chronicle process --workers 5")
    if totals["failed"]:
        print(f"  Retry failed:       chronicle process --retry-failed --workers 5")
        print(f"                      (first run `chronicle doctor` to verify "
              f"the failure reason is fixed)")


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
