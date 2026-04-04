"""Retroactively process all existing Claude Code sessions.

Supports parallel processing with configurable worker count (default 5).
Auto-stops the daemon before processing to avoid races, restarts it after.

Usage:
    chronicle batch                                  # process all new sessions
    chronicle batch --dry-run                        # preview without processing
    chronicle batch --project bada --workers 5       # one project (folder name)
    chronicle batch --force --workers 5              # reprocess everything
"""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import CHRONICLE_DIR, load_config, save_default_config
from .daemon import _is_running
from .extractor import extract_session
from .filtering import should_skip
from .storage import (
    mark_chronicled,
    write_session_record, append_to_chronicle, session_filename,
    rebuild_prompts_section,
)
from .summarizer import async_summarize_session


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def find_all_sessions(project_filter: str | None = None) -> list[tuple[str, Path]]:
    """Find all session JSONL files across all projects."""
    if not CLAUDE_PROJECTS.exists():
        return []

    sessions = []
    for project_dir in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter not in project_dir.name:
            continue

        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            # Skip subagent files
            if "subagents" in str(jsonl_file):
                continue
            sessions.append((project_dir.name, jsonl_file))

    return sessions


async def _process_one(digest, semaphore):
    """Process a single session under the concurrency semaphore."""
    async with semaphore:
        sid = digest.session_id[:8]
        turns = digest.total_turns
        # Show folder name from slug: -home-synesis-bada → bada
        parts = digest.project_slug.lstrip("-").split("-")
        project_name = parts[-1] if parts else digest.project_slug
        print(f"  [{project_name}/{sid}] starting ({turns} turns, {len(digest.user_prompts)} prompts)...")

        start = time.time()

        # Run summarization with periodic progress dots
        task = asyncio.create_task(async_summarize_session(digest))
        while not task.done():
            await asyncio.sleep(15)
            if not task.done():
                elapsed = int(time.time() - start)
                print(f"  [{project_name}/{sid}] still processing... ({elapsed}s)")

        elapsed = int(time.time() - start)
        entry = task.result()
        if entry.is_error:
            print(f"  [{project_name}/{sid}] error after {elapsed}s")
        elif entry.is_empty:
            print(f"  [{project_name}/{sid}] no decisions ({elapsed}s)")
        else:
            print(f"  [{project_name}/{sid}] done ({elapsed}s) — {len(entry.decisions)} decisions")
        return entry


async def async_batch_process(
    project_filter: str | None = None,
    dry_run: bool = False,
    workers: int = 5,
    force: bool = False,
):
    """Process all existing sessions with parallel workers."""
    save_default_config()
    config = load_config()
    sessions = find_all_sessions(project_filter)

    print(f"Found {len(sessions)} session files across "
          f"{len(set(s[0] for s in sessions))} projects\n")

    # Phase 1: Extract all digests (sync, fast)
    eligible = []
    skip_count = 0
    already_done = 0

    for project_slug, jsonl_path in sessions:
        try:
            digest = extract_session(str(jsonl_path))
        except Exception as e:
            print(f"  SKIP {jsonl_path.stem[:8]}: extraction error: {e}")
            skip_count += 1
            continue

        reason = should_skip(digest, config, force=force)
        if reason:
            if reason == "already chronicled":
                already_done += 1
            else:
                skip_count += 1
            continue

        eligible.append(digest)

    if dry_run:
        for digest in eligible:
            print(f"  WOULD PROCESS: {digest.project_slug}")
            print(f"    Session: {digest.session_id[:8]}")
            print(f"    Turns: {digest.total_turns}, Prompts: {len(digest.user_prompts)}")
            print(f"    Time: {digest.start_time[:19]} -> {digest.end_time[:19]}")
            if digest.user_prompts:
                print(f"    First prompt: {digest.user_prompts[0].text[:80]}...")
            print()
        print(f"\nDRY RUN Summary:")
        print(f"  Would process: {len(eligible)}")
        print(f"  Skipped (filtered): {skip_count}")
        print(f"  Already chronicled: {already_done}")
        if already_done:
            print(f"\n  View all sessions: chronicle rewind")
        return

    if not eligible:
        print("Nothing to process.")
        print(f"  Skipped: {skip_count}, Already done: {already_done}")
        if already_done:
            from .config import project_chronicle_dir
            print("\nAlready chronicled sessions:")
            for project_slug, jsonl_path in sessions:
                sessions_dir = project_chronicle_dir(project_slug) / "sessions"
                if sessions_dir.exists():
                    for md in sorted(sessions_dir.glob("*.md"), reverse=True):
                        with open(md, errors="ignore") as f:
                            first_line = f.readline().rstrip("\n")
                        title = first_line[2:] if first_line.startswith("# ") else md.stem
                        print(f"  {title}")
                        print(f"    vim {md}")
                    break  # only show first project match
        return

    # Sort by start_time so chronicle.md entries are chronological
    eligible.sort(key=lambda d: d.start_time)

    # Phase 2: Summarize in parallel — every session goes through the LLM
    print(f"Processing {len(eligible)} sessions with {workers} workers...\n")
    semaphore = asyncio.Semaphore(workers)
    tasks = [_process_one(digest, semaphore) for digest in eligible]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Phase 3: Write results (sync, fast — chronological order preserved)
    process_count = 0
    error_count = 0

    for digest, result in zip(eligible, results):
        if isinstance(result, Exception):
            print(f"  ERROR {digest.session_id[:8]}: {result}")
            error_count += 1
            continue

        entry = result
        if entry.is_error:
            print(f"  RETRY-LATER {digest.session_id[:8]}: transient failure")
            error_count += 1
            continue

        # Every session gets a record — even empty ones
        write_session_record(entry, digest.project_slug)
        append_to_chronicle(entry, digest.project_slug)
        mark_chronicled(digest.session_id, digest.end_time)
        process_count += 1

        from .config import project_chronicle_dir
        full_path = project_chronicle_dir(digest.project_slug) / "sessions" / session_filename(entry)
        print(f"  -> vim {full_path}")

    # Rebuild combined prompts section and show chronicle.md path
    if process_count:
        from .config import project_chronicle_dir
        projects_done = sorted(set(d.project_slug for d in eligible))
        print()
        for slug in projects_done:
            rebuild_prompts_section(slug)
            chronicle_path = project_chronicle_dir(slug) / "chronicle.md"
            if chronicle_path.exists():
                with open(chronicle_path) as f:
                    lines = sum(1 for _ in f)
                print(f"  Chronicle: vim {chronicle_path} ({lines} lines)")

    print(f"\nSummary:")
    print(f"  Processed: {process_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Already chronicled: {already_done}")
    if error_count:
        print(f"  Errors: {error_count}")
    if already_done:
        print(f"\n  View all sessions: chronicle rewind")


def main():
    parser = argparse.ArgumentParser(
        description="Batch process existing Claude Code sessions"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without processing")
    parser.add_argument("--project", type=str,
                        help="Filter to specific project (substring match)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of parallel workers (default: 5)")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess already-chronicled sessions")
    args = parser.parse_args()

    # Stop daemon to avoid duplicate processing — it'll respawn on next SessionStart
    daemon_was_running = False
    running, pid = _is_running()
    if running and not args.dry_run:
        os.kill(pid, signal.SIGTERM)
        daemon_was_running = True
        print(f"Stopped daemon (pid {pid}) to avoid duplicate processing.")
        time.sleep(1)  # let it shut down

    try:
        asyncio.run(async_batch_process(
            project_filter=args.project,
            dry_run=args.dry_run,
            workers=args.workers,
            force=args.force,
        ))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Already-processed sessions will be skipped on retry.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if daemon_was_running and not args.dry_run:
            log_file = CHRONICLE_DIR / "daemon.log"
            with open(log_file, "a") as log_fd:
                subprocess.Popen(
                    [sys.executable, "-m", "chronicle.daemon"],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=log_fd,
                )
            print("Restarted daemon.")


if __name__ == "__main__":
    main()
