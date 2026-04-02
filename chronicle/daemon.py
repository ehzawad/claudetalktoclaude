"""Background chronicler daemon.

Polls ~/.chronicle/events.jsonl for hook events. Uses a global debounce:
waits until ALL sessions across ALL projects have been quiet for 5 minutes
before processing anything. This prevents API contention when multiple
coding sessions are active on the same subscription.

Processes sessions in parallel (5 workers via asyncio). Writes per-session
markdown files and a cumulative chronicle.md per project.

Usage:
    python -m chronicle.daemon          # run in foreground
    python -m chronicle.daemon --bg     # daemonize
    python -m chronicle.daemon --stop   # stop running daemon
    python -m chronicle.daemon --status # check if running
"""

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

from .config import (
    CHRONICLE_DIR, EVENTS_FILE, OFFSET_FILE, PID_FILE,
    load_config, save_default_config,
)
from .extractor import extract_session
from .filtering import should_skip
from .storage import write_chronicle, already_chronicled
from .summarizer import async_summarize_session


def _read_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return int(OFFSET_FILE.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def _save_offset(offset: int):
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset))
    os.replace(str(tmp), str(OFFSET_FILE))


def _extract_and_filter(event: dict, config: dict):
    """Extract session and apply filters. Returns digest or None."""
    session_id = event.get("session_id", "")
    transcript_path = event.get("transcript_path", "")

    if not transcript_path or not Path(transcript_path).exists():
        return None

    try:
        digest = extract_session(transcript_path)
    except Exception as e:
        print(f"[chronicle] extraction failed: {e}", file=sys.stderr)
        return None

    reason = should_skip(digest, config)
    if reason:
        print(f"[chronicle] skipping {session_id[:8]}: {reason}")
        return None

    return digest


async def _async_process_one(event: dict, config: dict, semaphore: asyncio.Semaphore):
    """Process a single Stop event under the concurrency semaphore.

    Returns (digest, entry) for deferred chronological writing, or None.
    """
    digest = _extract_and_filter(event, config)
    if digest is None:
        return None

    async with semaphore:
        print(f"[chronicle] summarizing session {digest.session_id[:8]} "
              f"({digest.total_turns} turns, {len(digest.user_prompts)} prompts)...")
        entry = await async_summarize_session(digest)
        return (digest, entry)


async def _process_batch(events: list[tuple[str, dict]], config: dict) -> list[tuple[str, dict]]:
    """Process multiple Stop events concurrently, write in chronological order.

    Returns (session_id, event) pairs that should be retried on the next
    debounce cycle. Sessions that exceed max_retries are given up on.
    """
    concurrency = config.get("concurrency", 5)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _async_process_one(event, config, semaphore)
        for _, event in events
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    max_retries = config.get("max_retries", 3)
    pending_writes = []
    retry = []
    for (sid, ev), result in zip(events, results):
        if isinstance(result, Exception):
            print(f"[chronicle] error processing {sid[:8]}: {result}",
                  file=sys.stderr)
            retry.append((sid, ev))
        elif result is not None:
            pending_writes.append(result)

    pending_writes.sort(key=lambda pair: pair[0].start_time)
    for digest, entry in pending_writes:
        write_chronicle(entry, digest, max_retries=max_retries)
        if entry.is_error and not already_chronicled(digest.session_id, digest.end_time):
            for sid, ev in events:
                if sid == digest.session_id:
                    retry.append((sid, ev))
                    break

    return retry


def _read_new_events(offset: int) -> tuple[list[dict], int]:
    """Read events from the JSONL file starting at the given byte offset."""
    if not EVENTS_FILE.exists():
        return [], offset

    file_size = EVENTS_FILE.stat().st_size
    if offset > file_size:
        print(f"[chronicle] offset ({offset}) exceeds file size ({file_size}), resetting to 0")
        offset = 0

    events = []
    with open(EVENTS_FILE, "rb") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print("[chronicle] skipping malformed event line", file=sys.stderr)
                continue
        new_offset = f.tell()

    return events, new_offset


def _process_events(events: list[dict], pending_sessions: dict) -> bool:
    """Categorize hook events into pending_sessions.

    Returns True if any activity occurred (caller should reset debounce timer).
    """
    activity = False
    for event in events:
        event_name = event.get("hook_event_name", "")
        session_id = event.get("session_id", "")

        if event_name in ("UserPromptSubmit", "Stop", "SessionEnd"):
            activity = True

        if event_name in ("Stop", "SessionEnd") and session_id:
            existing = pending_sessions.get(session_id)
            if not existing or not existing.get("transcript_path"):
                pending_sessions[session_id] = event
        elif event_name == "UserPromptSubmit" and session_id:
            pending_sessions.pop(session_id, None)

    return activity


_lock_fd = None  # Module-level storage for the lock file descriptor.
                  # Raw int fds from os.open() aren't GC'd, but storing
                  # explicitly documents intent and prevents future breakage.


def _acquire_lock() -> bool:
    """Try to acquire singleton lock via PID file."""
    global _lock_fd
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        _lock_fd = os.open(str(PID_FILE), os.O_CREAT | os.O_WRONLY)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(_lock_fd, str(os.getpid()).encode())
        os.ftruncate(_lock_fd, len(str(os.getpid())))
        return True
    except (OSError, IOError):
        return False


def _is_running() -> tuple[bool, int | None]:
    """Check if a daemon is already running."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True, pid
    except (ValueError, OSError):
        return False, None


async def run_daemon_async():
    """Fully async main daemon loop."""
    save_default_config()

    if not _acquire_lock():
        running, pid = _is_running()
        if running:
            print(f"[chronicle] daemon already running (pid {pid})")
            sys.exit(1)

    print(f"[chronicle] daemon started (pid {os.getpid()})")

    # Handle graceful shutdown via asyncio event.
    # Use loop.add_signal_handler (not signal.signal) so that set() is
    # called safely from the event loop thread, not a signal handler context.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    config = load_config()
    poll_interval = config.get("poll_interval_seconds", 5)
    offset = _read_offset()
    pending_sessions = {}  # session_id -> event dict
    last_activity = 0.0  # timestamp of ANY event from ANY session

    while not stop_event.is_set():
        try:
            config = load_config()
            events, new_offset = _read_new_events(offset)

            if _process_events(events, pending_sessions):
                last_activity = time.time()

            offset = new_offset

            # Only process when ALL sessions have been quiet for quiet_minutes
            quiet_minutes = config.get("quiet_minutes", 5)
            now = time.time()
            global_quiet = (now - last_activity) >= (quiet_minutes * 60) if last_activity else False

            if global_quiet and pending_sessions:
                to_process = list(pending_sessions.items())
                pending_sessions.clear()
                try:
                    retry = await _process_batch(to_process, config)
                    if retry:
                        for sid, ev in retry:
                            pending_sessions[sid] = ev
                        last_activity = time.time()
                except Exception as e:
                    print(f"[chronicle] batch error: {e}", file=sys.stderr)
                    for sid, ev in to_process:
                        pending_sessions[sid] = ev
                    last_activity = time.time()

            # Only persist offset when pending_sessions is empty. While
            # sessions are waiting for the debounce, they exist only in
            # memory. Advancing the on-disk offset before they're processed
            # means a daemon crash loses them permanently.
            if not pending_sessions:
                _save_offset(offset)

        except Exception as e:
            print(f"[chronicle] loop error: {e}", file=sys.stderr)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass  # Normal — poll interval elapsed, loop again

    print("[chronicle] daemon stopped")


def run_daemon():
    """Entry point — runs the async daemon loop."""
    asyncio.run(run_daemon_async())


def main():
    parser = argparse.ArgumentParser(description="Decision Chronicle daemon")
    parser.add_argument("--bg", action="store_true", help="Run as background daemon")
    parser.add_argument("--stop", action="store_true", help="Stop running daemon")
    parser.add_argument("--status", action="store_true", help="Check daemon status")
    args = parser.parse_args()

    if args.status:
        running, pid = _is_running()
        if running:
            print(f"Chronicle daemon is running (pid {pid})")
        else:
            print("Chronicle daemon is not running")
        sys.exit(0)

    if args.stop:
        running, pid = _is_running()
        if running:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (pid {pid})")
        else:
            print("No daemon running")
        sys.exit(0)

    if args.bg:
        pid = os.fork()
        if pid > 0:
            print(f"[chronicle] daemon started in background (pid {pid})")
            sys.exit(0)
        # Child process — full detach
        os.setsid()
        # Redirect stdin to /dev/null, stdout/stderr to log file
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, sys.stdin.fileno())
        os.close(devnull)
        log_file = CHRONICLE_DIR / "daemon.log"
        log_fd = open(log_file, "a")
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())
        log_fd.close()

    run_daemon()


if __name__ == "__main__":
    main()
