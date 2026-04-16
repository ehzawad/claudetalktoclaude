"""Process existing Claude Code sessions into chronicle records.

This is the workhorse for BOTH modes:
- Foreground (default): the only way sessions get summarized. Run on demand.
- Background: still works; pauses the launchd/systemd service via service.py
  and holds ~/.chronicle/processing.lock so the daemon can't race.

Supports parallel processing (default 5 workers), --force to reprocess
successful sessions, and --retry-failed to retry sessions whose
.failed/<hash>.json marker is terminal.

Usage:
    chronicle process                                  # process pending sessions
    chronicle process --dry-run                        # preview without processing
    chronicle process --project bada --workers 5       # substring-match one project
    chronicle process --force --workers 5              # reprocess successes
    chronicle process --retry-failed --workers 5       # retry terminal failures
"""

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import CHRONICLE_DIR, CLAUDE_PROJECTS, load_config, save_default_config
from .extractor import extract_session
from .filtering import should_skip
from .locks import processing_lock
from .mode import is_background_mode
from .service import pause_service, resume_service
from .storage import (
    write_chronicle, session_filename,
    rebuild_prompts_section,
)
from .summarizer import async_summarize_session


# Interval at which `_process_one` emits a "still processing..." heartbeat
# for long-running summarizations. A completed task short-circuits the wait,
# so fast completions return immediately (they don't sit idle for this long).
PROGRESS_INTERVAL_SECONDS = 15


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

        # Run summarization with periodic progress heartbeat. asyncio.wait
        # returns as soon as the task completes OR the interval elapses —
        # whichever is sooner — so a fast completion is observed immediately.
        task = asyncio.create_task(async_summarize_session(digest))
        while not task.done():
            done, _pending = await asyncio.wait(
                {task}, timeout=PROGRESS_INTERVAL_SECONDS,
            )
            if not done:
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
    retry_failed: bool = False,
):
    """Process all existing sessions with parallel workers."""
    save_default_config()
    config = load_config()
    # Honor ~/.chronicle/config.json's max_retries; the daemon does. Without
    # this, `chronicle process` silently used the storage.write_chronicle
    # default (3) regardless of config.
    max_retries = int(config.get("max_retries", 3))
    sessions = find_all_sessions(project_filter)

    print(f"Found {len(sessions)} session files across "
          f"{len(set(s[0] for s in sessions))} projects\n")

    # Phase 1: Extract all digests (sync, fast)
    eligible = []
    skip_count = 0
    already_done = 0
    failed_skipped = 0

    for project_slug, jsonl_path in sessions:
        try:
            digest = extract_session(str(jsonl_path))
        except Exception as e:
            print(f"  SKIP {jsonl_path.stem[:8]}: extraction error: {e}")
            skip_count += 1
            continue

        reason = should_skip(digest, config, force=force, retry_failed=retry_failed)
        if reason:
            if reason == "already chronicled":
                already_done += 1
            elif reason == "terminal failure":
                failed_skipped += 1
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
        if failed_skipped:
            print(f"  Terminal failures (use --retry-failed to retry): {failed_skipped}")
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
        # Python 3.14: CancelledError is a BaseException, not Exception.
        # Re-raise so Ctrl-C / SIGTERM propagates instead of being logged
        # as a per-session error and quietly dropped.
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        if isinstance(result, Exception):
            print(f"  ERROR {digest.session_id[:8]}: {result}")
            error_count += 1
            continue

        entry = result
        # Use the same write path as the daemon — handles retries,
        # cost persistence, and empty sessions consistently
        write_chronicle(entry, digest, max_retries=max_retries)
        if entry.is_error:
            print(f"  RETRY-LATER {digest.session_id[:8]}: transient failure")
            error_count += 1
            continue

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
    if failed_skipped:
        print(f"  Terminal failures (use --retry-failed to retry): {failed_skipped}")
    if error_count:
        print(f"  Errors: {error_count}")
    if already_done:
        print(f"\n  View all sessions: chronicle rewind")


def main():
    parser = argparse.ArgumentParser(
        description="Process existing Claude Code sessions"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without processing")
    parser.add_argument("--project", type=str,
                        help="Filter to specific project (substring match)")
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of parallel workers (default: 5)")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess already-chronicled (success) sessions")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Retry sessions in .failed/ terminal state "
                             "(e.g. after fixing a PATH or config issue)")
    args = parser.parse_args()

    # In background mode, pause the service manager so launchd/systemd
    # doesn't respawn the daemon while we process. The processing lock
    # is the hard correctness boundary; service pause is hygiene.
    paused = False
    if is_background_mode() and not args.dry_run:
        paused = pause_service()
        if paused:
            print("Paused background daemon service (will resume after processing).")

    try:
        # Processing lock blocks until acquired. If the daemon is mid-batch
        # (unlikely after pause_service, but possible before SIGTERM lands),
        # we wait for it to finish cleanly.
        with processing_lock(blocking=True):
            asyncio.run(async_batch_process(
                project_filter=args.project,
                dry_run=args.dry_run,
                workers=args.workers,
                force=args.force,
                retry_failed=args.retry_failed,
            ))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Already-processed sessions will be skipped on retry.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if paused:
            resume_service()
            print("Resumed background daemon service.")


if __name__ == "__main__":
    main()
