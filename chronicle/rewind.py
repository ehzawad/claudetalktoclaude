"""Rewind through chronicle sessions — navigate project history like Claude Code's /rewind.

Usage:
    chronicle rewind                    # show numbered session list
    chronicle rewind 1                  # view session #1's full details
    chronicle rewind --since 3          # show sessions from #3 to latest
    chronicle rewind --summary 2        # AI-summarize sessions #2 through latest
    chronicle rewind --project NAME     # target a specific project
    chronicle rewind --diff 2           # show what was NEW in session #2
"""

import argparse
import os
import re
import sys
from pathlib import Path


from .config import PROJECTS_DIR


def _find_project_dir(project: str | None = None) -> Path | None:
    """Resolve which project to rewind through."""
    if not PROJECTS_DIR.exists():
        return None

    if project:
        # Partial match on slug
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir() and project in d.name:
                return d
        return None

    # Default: current working directory
    cwd = os.getcwd()
    slug = cwd.replace("/", "-")
    candidate = PROJECTS_DIR / slug
    if candidate.exists():
        return candidate

    # Try partial match on the last component
    dir_name = os.path.basename(cwd)
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and dir_name in d.name:
            return d

    return None


def _load_sessions(project_dir: Path) -> list[dict]:
    """Load all session files, sorted chronologically (oldest first = #1)."""
    sessions_dir = project_dir / "sessions"
    if not sessions_dir.exists():
        return []

    sessions = []
    for md_file in sorted(sessions_dir.glob("*.md")):
        content = md_file.read_text()

        # Extract metadata
        title_match = re.search(r"^# (.+)", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else md_file.stem

        date_match = re.search(r"\*\*Date\*\*:\s*([^|]+)", content)
        date = date_match.group(1).strip() if date_match else "unknown"

        session_match = re.search(r"\*\*Session\*\*:\s*(\w+)", content)
        session_id = session_match.group(1).strip() if session_match else md_file.stem[:8]

        branch_match = re.search(r"\*\*Branch\*\*:\s*([^|]+)", content)
        branch = branch_match.group(1).strip() if branch_match else ""

        turns_match = re.search(r"\*\*Turns\*\*:\s*(\d+)", content)
        turns = int(turns_match.group(1)) if turns_match else 0

        summary_match = re.search(r"## Summary\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        if not summary_match:
            summary_match = re.search(r"## What happened\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        summary = summary_match.group(1).strip() if summary_match else ""

        # Count decisions
        n_decisions = len(re.findall(r"^### ", content, re.MULTILINE))

        # Extract decision titles
        decisions = re.findall(r"^### (.+)", content, re.MULTILINE)

        # Extract open questions
        oq_section = re.search(r"## Open questions\n\n((?:- .+\n?)+)", content)
        open_questions = []
        if oq_section:
            open_questions = re.findall(r"^- (.+)", oq_section.group(1), re.MULTILINE)

        # Extract files changed
        fc_section = re.search(r"## Files changed\n\n((?:- .+\n?)+)", content)
        files_changed = []
        if fc_section:
            files_changed = re.findall(r"`([^`]+)`", fc_section.group(1))

        sessions.append({
            "number": 0,  # filled in below
            "path": md_file,
            "title": title,
            "date": date,
            "session_id": session_id,
            "branch": branch,
            "turns": turns,
            "summary": summary,
            "n_decisions": n_decisions,
            "decisions": decisions,
            "open_questions": open_questions,
            "files_changed": files_changed,
            "content": content,
        })

    # Number them chronologically (oldest = 1)
    for i, s in enumerate(sessions, 1):
        s["number"] = i

    return sessions


def show_session_list(sessions: list[dict], project_dir: Path):
    """Display numbered session list — the rewind menu."""
    project_name = project_dir.name.rsplit("-", 1)[-1] if "-" in project_dir.name else project_dir.name
    print(f"  Chronicle: {project_name} ({len(sessions)} sessions)\n")
    print(f"  {'#':>3}  {'Date':16}  {'Turns':>5}  {'Dec':>3}  Title")
    print(f"  {'─'*3}  {'─'*16}  {'─'*5}  {'─'*3}  {'─'*50}")

    for s in sessions:
        marker = "→" if s["number"] == len(sessions) else " "
        print(f" {marker}{s['number']:>3}  {s['date'][:16]:16}  {s['turns']:>5}  {s['n_decisions']:>3}  {s['title'][:55]}")

    print()
    print(f"  View session:      chronicle rewind <N>")
    print(f"  View range:        chronicle rewind --since <N>")
    print(f"  Summarize range:   chronicle rewind --summary <N>")
    print(f"  Diff a session:    chronicle rewind --diff <N>")


def show_session(session: dict):
    """Display a single session's full content."""
    n = session["number"]
    print(f"  Session #{n}: {session['title']}")
    print(f"  {'─' * 70}")
    print(f"  Date: {session['date']}  |  Branch: {session['branch']}  |  Turns: {session['turns']}  |  Decisions: {session['n_decisions']}")
    print()

    if session["summary"]:
        print(f"  Summary:")
        for line in session["summary"].split("\n"):
            print(f"    {line}")
        print()

    if session["decisions"]:
        print(f"  Decisions:")
        for d in session["decisions"]:
            # Strip trailing status tags like " _rejected_"
            clean = re.sub(r"\s+_\w+_$", "", d)
            print(f"    • {clean}")
        print()

    if session["open_questions"]:
        print(f"  Open questions:")
        for q in session["open_questions"]:
            print(f"    ? {q}")
        print()

    if session["files_changed"]:
        print(f"  Files changed:")
        for f in session["files_changed"]:
            print(f"    ~ {f}")
        print()

    print(f"  Full details: vim {session['path']}")
    print()


def show_since(sessions: list[dict], start_num: int):
    """Show sessions from start_num to the latest — like reading forward from a checkpoint."""
    subset = [s for s in sessions if s["number"] >= start_num]
    if not subset:
        print(f"No sessions from #{start_num} onward.")
        return

    print(f"  Sessions #{start_num} → #{sessions[-1]['number']} ({len(subset)} sessions)\n")

    for s in subset:
        sep = "─" * 70
        print(f"  ┌─ #{s['number']} {s['date'][:16]}  {s['title']}")
        print(f"  │")
        if s["summary"]:
            for line in s["summary"][:200].split("\n"):
                print(f"  │  {line}")
        if s["decisions"]:
            print(f"  │")
            for d in s["decisions"]:
                clean = re.sub(r"\s+_\w+_$", "", d)
                print(f"  │  • {clean}")
        if s["open_questions"]:
            print(f"  │")
            for q in s["open_questions"]:
                print(f"  │  ? {q}")
        print(f"  └{'─' * 70}")
        print()


def show_diff(sessions: list[dict], target_num: int):
    """Show what was NEW in a specific session vs cumulative state before it."""
    target = next((s for s in sessions if s["number"] == target_num), None)
    if not target:
        print(f"Session #{target_num} not found.")
        return

    # Gather cumulative state from all sessions before this one
    prior_decisions = set()
    prior_files = set()
    prior_questions = set()
    for s in sessions:
        if s["number"] >= target_num:
            break
        prior_decisions.update(s["decisions"])
        prior_files.update(s["files_changed"])
        prior_questions.update(s["open_questions"])

    new_decisions = [d for d in target["decisions"] if d not in prior_decisions]
    new_files = [f for f in target["files_changed"] if f not in prior_files]
    resolved_questions = [q for q in prior_questions if q not in target.get("open_questions", [])]

    print(f"  Diff: Session #{target_num} — {target['title']}")
    print(f"  {'─' * 70}")
    print()

    if target_num == 1:
        print(f"  (First session — everything is new)")
        print()
        show_session(target)
        return

    print(f"  NEW decisions ({len(new_decisions)}):")
    if new_decisions:
        for d in new_decisions:
            clean = re.sub(r"\s+_\w+_$", "", d)
            print(f"    + {clean}")
    else:
        print(f"    (none — all decisions were continuations of prior work)")
    print()

    print(f"  NEW files touched ({len(new_files)}):")
    if new_files:
        for f in new_files:
            print(f"    + {f}")
    else:
        print(f"    (same files as before)")
    print()

    if resolved_questions:
        print(f"  RESOLVED from prior sessions ({len(resolved_questions)}):")
        for q in resolved_questions:
            print(f"    ✓ {q}")
        print()

    if target["open_questions"]:
        new_q = [q for q in target["open_questions"] if q not in prior_questions]
        if new_q:
            print(f"  NEW open questions ({len(new_q)}):")
            for q in new_q:
                print(f"    ? {q}")
            print()


def summarize_range(sessions: list[dict], start_num: int):
    """AI-summarize sessions from start_num through latest."""
    import asyncio

    from .claude_cli import spawn_claude

    subset = [s for s in sessions if s["number"] >= start_num]
    if not subset:
        print(f"No sessions from #{start_num} onward.")
        return

    # Build a condensed transcript of the sessions
    transcript_parts = []
    for s in subset:
        transcript_parts.append(f"## Session #{s['number']}: {s['title']} ({s['date']})")
        if s["summary"]:
            transcript_parts.append(s["summary"])
        if s["decisions"]:
            transcript_parts.append("Decisions:")
            for d in s["decisions"]:
                transcript_parts.append(f"  - {d}")
        if s["open_questions"]:
            transcript_parts.append("Open questions:")
            for q in s["open_questions"]:
                transcript_parts.append(f"  - {q}")
        transcript_parts.append("")

    transcript = "\n".join(transcript_parts)

    prompt = f"""Summarize these {len(subset)} chronicle sessions into a single concise narrative.
Focus on: what the overall arc was, what key decisions persisted, what got resolved,
and what's still open. Write as a developer catching up on project history.
Keep it under 300 words.

{transcript}"""

    from .config import load_config
    config = load_config()
    model = config.get("model", "opus")

    print(f"  Summarizing sessions #{start_num}–#{sessions[-1]['number']}...\n")

    async def _summarize():
        fallback = config.get("fallback_model", "sonnet")
        res = await spawn_claude(
            prompt=prompt, model=model, fallback_model=fallback,
            effort="low", timeout=300,
        )
        if not res.ok:
            return None
        return (res.stdout_json or {}).get("result", "")

    try:
        result = asyncio.run(_summarize())
        if result:
            print(f"  Summary (sessions #{start_num}–#{sessions[-1]['number']}):")
            print(f"  {'─' * 70}")
            for line in result.strip().split("\n"):
                print(f"  {line}")
            print()
        else:
            print("  Summarization failed. Falling back to session list.\n")
            show_since(sessions, start_num)
    except Exception as e:
        print(f"  Summarization error: {e}. Falling back to session list.\n")
        show_since(sessions, start_num)


def delete_session_by_number(sessions: list[dict], project_dir: Path, target_num: int):
    """Delete a single session by its rewind number."""
    target = next((s for s in sessions if s["number"] == target_num), None)
    if not target:
        print(f"Session #{target_num} not found.")
        return

    from .storage import delete_session
    slug = project_dir.name
    delete_session(target["path"], slug)
    print(f"  Deleted session #{target_num}: {target['title']}")
    print(f"  Removed: {target['path'].name}")


def prune_empty_sessions(sessions: list[dict], project_dir: Path):
    """Delete all sessions with 0 decisions (trivial/abandoned sessions)."""
    from .storage import delete_session
    slug = project_dir.name

    empty = [s for s in sessions if s["n_decisions"] == 0]
    if not empty:
        print("No empty sessions to prune.")
        return

    print(f"Pruning {len(empty)} session(s) with 0 decisions:\n")
    for s in empty:
        print(f"  #{s['number']:>3}  {s['date'][:16]}  {s['title'][:55]}")

    print()
    confirm = input(f"Delete {len(empty)} session(s)? [y/N] ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    for s in empty:
        delete_session(s["path"], slug)
        print(f"  Deleted #{s['number']}: {s['title']}")

    print(f"\nPruned {len(empty)} sessions. {len(sessions) - len(empty)} remaining.")


def main():
    parser = argparse.ArgumentParser(
        description="Rewind through chronicle sessions",
        usage="chronicle rewind [N] [--since N] [--summary N] [--diff N] [--delete N] [--prune] [--project NAME]",
    )
    parser.add_argument("session", nargs="?", type=int, help="Session number to view")
    parser.add_argument("--since", type=int, metavar="N", help="Show sessions from #N onward")
    parser.add_argument("--summary", type=int, metavar="N", help="AI-summarize sessions from #N onward")
    parser.add_argument("--diff", type=int, metavar="N", help="Show what was new in session #N")
    parser.add_argument("--delete", type=int, metavar="N", help="Delete session #N")
    parser.add_argument("--prune", action="store_true", help="Delete all sessions with 0 decisions")
    parser.add_argument("--project", type=str, help="Target a specific project (partial match)")
    args = parser.parse_args()

    project_dir = _find_project_dir(args.project)
    if not project_dir:
        if args.project:
            print(f"No chronicles found for '{args.project}'.")
        else:
            print(f"No chronicles found for current directory.")
            print(f"Run: chronicle process --workers 5")
        sys.exit(1)

    sessions = _load_sessions(project_dir)
    if not sessions:
        print(f"No session records in {project_dir.name}.")
        sys.exit(1)

    if args.delete is not None:
        if args.delete < 1 or args.delete > len(sessions):
            print(f"Session #{args.delete} out of range (1–{len(sessions)}).")
            sys.exit(1)
        delete_session_by_number(sessions, project_dir, args.delete)
    elif args.prune:
        prune_empty_sessions(sessions, project_dir)
    elif args.session is not None:
        if args.session < 1 or args.session > len(sessions):
            print(f"Session #{args.session} out of range (1–{len(sessions)}).")
            sys.exit(1)
        show_session(sessions[args.session - 1])
    elif args.since is not None:
        show_since(sessions, args.since)
    elif args.summary is not None:
        summarize_range(sessions, args.summary)
    elif args.diff is not None:
        if args.diff < 1 or args.diff > len(sessions):
            print(f"Session #{args.diff} out of range (1–{len(sessions)}).")
            sys.exit(1)
        show_diff(sessions, args.diff)
    else:
        show_session_list(sessions, project_dir)


if __name__ == "__main__":
    main()
