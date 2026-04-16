"""Background chronicler daemon.

Only processes sessions when `processing_mode=background` in
~/.chronicle/config.json. In foreground mode (the default) this daemon
does NOT exist; if a stale launchd/systemd service keeps it alive after
the mode was flipped, it idles without reading events, scanning, or
spawning `claude -p` — avoiding a KeepAlive restart loop.

When background mode is active:
- Polls ~/.chronicle/events.jsonl for hook events.
- Uses a global debounce — waits until ALL sessions across ALL projects
  have been quiet for `quiet_minutes` (default 5) before processing.
  Prevents contention with active coding sessions on the same subscription.
- Runs a periodic scanner (default every 30 min) that queues any JSONL
  under ~/.claude/projects/ without an existing .processed / .failed marker.
- Processes in parallel (default 5 workers via asyncio.Semaphore).
- Holds ~/.chronicle/processing.lock across its batch so `chronicle process`
  can't race. Terminates in-flight claude subprocesses on shutdown.
- Writes per-session .md + cumulative chronicle.md; marks .processed/<hash>
  on success, .failed/<hash>.json on transient / parse / terminal failure.

Usage:
    python -m chronicle.daemon          # run in foreground (this process)
    python -m chronicle.daemon --bg     # fork + setsid daemonize
    python -m chronicle.daemon --stop   # SIGTERM the running daemon
    python -m chronicle.daemon --status # check if running

Normal mode switching is `chronicle install-daemon` / `uninstall-daemon`,
which manages the launchd plist / systemd unit. This module is the raw
process; most users never invoke it directly.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from .claude_cli import terminate_active_subprocesses
from .config import (
    chronicle_dir, claude_projects, events_file, offset_file, pid_file,
    load_config, save_default_config,
)
from .extractor import extract_session
from .filtering import should_skip
from .locks import (
    acquire_daemon_lock, daemon_lock_still_valid, daemon_is_running,
    processing_lock,
)
from .mode import is_background_mode
from .storage import (
    is_succeeded, is_terminal_failure, write_chronicle,
)
from .summarizer import async_summarize_session


def _read_offset() -> int:
    if offset_file().exists():
        try:
            return int(offset_file().read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def _save_offset(offset: int):
    tmp = offset_file().with_suffix(".tmp")
    tmp.write_text(str(offset))
    os.replace(str(tmp), str(offset_file()))


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
        # Python 3.14: CancelledError is a BaseException, not Exception.
        # Re-raise so shutdown propagates and we don't silently drop sessions.
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        if isinstance(result, Exception):
            print(f"[chronicle] error processing {sid[:8]}: {result}",
                  file=sys.stderr)
            retry.append((sid, ev))
        elif result is not None:
            pending_writes.append(result)

    pending_writes.sort(key=lambda pair: pair[0].start_time)
    for digest, entry in pending_writes:
        write_chronicle(entry, digest, max_retries=max_retries)
        # Requeue if the session still has no terminal outcome — i.e.,
        # transient failure that hasn't hit max retries yet, or an INFRA
        # error that doesn't count against retries.
        if (entry.is_error
                and not is_succeeded(digest.session_id)
                and not is_terminal_failure(digest.session_id)):
            for sid, ev in events:
                if sid == digest.session_id:
                    retry.append((sid, ev))
                    break

    return retry


def _read_new_events(offset: int) -> tuple[list[dict], int]:
    """Read events from the JSONL file starting at the given byte offset.

    Never advance past a partial (unterminated) final line — if the hook
    is mid-write when we read, we'd otherwise permanently skip the event.
    Malformed COMPLETE lines (have a trailing \\n but fail json.loads) are
    skipped with the offset advancing past them so they don't re-appear.
    """
    if not events_file().exists():
        return [], offset

    file_size = events_file().stat().st_size
    if offset > file_size:
        print(f"[chronicle] offset ({offset}) exceeds file size ({file_size}), resetting to 0")
        offset = 0

    with open(events_file(), "rb") as f:
        f.seek(offset)
        buf = f.read()

    events = []
    pos = 0
    last_complete_end = 0  # offset past the last \n we processed
    while pos < len(buf):
        nl = buf.find(b"\n", pos)
        if nl == -1:
            # Partial trailing line — hold it back for the next tick.
            break
        line = buf[pos:nl].strip()
        pos = nl + 1
        last_complete_end = pos
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            print("[chronicle] skipping malformed event line", file=sys.stderr)
            continue

    new_offset = offset + last_complete_end
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


# Singleton and inode-validation helpers live in chronicle.locks now.
# Keep these thin shims so external imports (batch.py, __main__.py) stay stable.

def _acquire_lock() -> bool:
    return acquire_daemon_lock()


def _lock_still_valid() -> bool:
    return daemon_lock_still_valid()


def _is_running() -> tuple[bool, int | None]:
    return daemon_is_running()


def _scan_for_unprocessed(pending_sessions: dict, config: dict) -> int:
    """Scan ~/.claude/projects/ for sessions with no events that aren't chronicled.

    Adds synthetic events for discovered sessions so the normal debounce +
    processing pipeline handles them. Returns count of newly queued sessions.
    """
    if not claude_projects().exists():
        return 0

    queued = 0
    for project_dir in claude_projects().iterdir():
        if not project_dir.is_dir():
            continue
        skip_projects = config.get("skip_projects", [])
        if any(sp in project_dir.name for sp in skip_projects):
            continue

        quiet_seconds = config.get("quiet_minutes", 5) * 60
        now = time.time()

        for jsonl_file in project_dir.glob("*.jsonl"):
            if "subagents" in str(jsonl_file):
                continue

            # Skip files still being written — mtime must be older than
            # quiet_minutes to avoid chronicling an active session
            try:
                age = now - jsonl_file.stat().st_mtime
                if age < quiet_seconds:
                    continue
            except OSError:
                continue

            session_id = jsonl_file.stem
            if session_id in pending_sessions:
                continue

            if is_succeeded(session_id):
                continue

            if is_terminal_failure(session_id):
                # Given up on this one — user retries via
                # `chronicle process --retry-failed`.
                continue

            pending_sessions[session_id] = {
                "session_id": session_id,
                "transcript_path": str(jsonl_file),
                "cwd": project_dir.name,
                "hook_event_name": "Stop",  # synthetic
                "source": "scan",
            }
            queued += 1

    return queued


async def run_daemon_async():
    """Fully async main daemon loop.

    Behavior:
    - Honors processing_mode=foreground by idling (not exiting) if a
      stale service manager keeps respawning us.
    - Acquires the singleton fcntl lock; exits with diagnostic on failure.
    - Reads events, scans for un-evented sessions, debounces 5 minutes,
      then processes under the processing lock so `chronicle process`
      never races us.
    - On SIGTERM/SIGINT/SIGHUP: sets stop_event, terminates in-flight
      claude subprocesses via the registry in claude_cli.
    """
    save_default_config()

    if not _acquire_lock():
        running, pid = _is_running()
        if running:
            print(f"[chronicle] daemon already running (pid {pid})")
            sys.exit(1)
        print("[chronicle] could not acquire singleton lock and no running "
              "daemon detected — check permissions on "
              f"{pid_file()} and {chronicle_dir()}", file=sys.stderr)
        sys.exit(2)

    print(f"[chronicle] daemon started (pid {os.getpid()})")

    # Graceful shutdown via asyncio event — add_signal_handler so .set()
    # runs in the loop thread, not in signal-handler context.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop_event.set)

    config = load_config()
    poll_interval = config.get("poll_interval_seconds", 5)
    offset = _read_offset()
    pending_sessions: dict = {}
    last_activity = 0.0
    last_scan = 0.0
    idle_printed_once = False

    try:
        while not stop_event.is_set():
            try:
                # Self-disable if config says foreground. Idle rather than
                # exit: with launchd KeepAlive, exit would be a restart loop.
                if not is_background_mode():
                    if not idle_printed_once:
                        print("[chronicle] processing_mode=foreground — "
                              "daemon idle (no auto-processing); run "
                              "`chronicle uninstall-daemon` to remove this service",
                              file=sys.stderr)
                        idle_printed_once = True
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                idle_printed_once = False

                if not _lock_still_valid():
                    print("[chronicle] PID file replaced — another daemon took over, exiting")
                    break

                config = load_config()
                events, new_offset = _read_new_events(offset)

                if _process_events(events, pending_sessions):
                    last_activity = time.time()

                offset = new_offset

                scan_interval = config.get("scan_interval_minutes", 30) * 60
                now = time.time()
                if now - last_scan >= scan_interval:
                    queued = _scan_for_unprocessed(pending_sessions, config)
                    if queued:
                        print(f"[chronicle] scan found {queued} un-chronicled session(s)")
                        if not last_activity:
                            last_activity = now
                    last_scan = now

                quiet_minutes = config.get("quiet_minutes", 5)
                now = time.time()
                global_quiet = (
                    (now - last_activity) >= (quiet_minutes * 60)
                    if last_activity else False
                )

                if global_quiet and pending_sessions:
                    to_process = list(pending_sessions.items())
                    pending_sessions.clear()
                    try:
                        # Processing lock: prevents race with `chronicle process`.
                        with processing_lock(blocking=False) as acquired:
                            if not acquired:
                                print("[chronicle] processing lock held by another "
                                      "process (likely chronicle process) — "
                                      "deferring", file=sys.stderr)
                                for sid, ev in to_process:
                                    pending_sessions[sid] = ev
                                last_activity = time.time()
                            else:
                                retry = await _process_batch(to_process, config)
                                if retry:
                                    for sid, ev in retry:
                                        pending_sessions[sid] = ev
                                    last_activity = time.time()
                    except asyncio.CancelledError:
                        for sid, ev in to_process:
                            pending_sessions[sid] = ev
                        raise
                    except Exception as e:
                        print(f"[chronicle] batch error: {e}", file=sys.stderr)
                        for sid, ev in to_process:
                            pending_sessions[sid] = ev
                        last_activity = time.time()

                # Only persist offset when pending is empty, so a crash
                # during debounce doesn't drop sessions.
                if not pending_sessions:
                    _save_offset(offset)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[chronicle] loop error: {e}", file=sys.stderr)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        # Cleanly terminate in-flight claude subprocesses before we exit.
        terminated = await terminate_active_subprocesses(grace_seconds=5.0)
        if terminated.get("terminated"):
            print(f"[chronicle] terminated {terminated['terminated']} in-flight "
                  f"claude subprocess(es), killed {terminated['killed']}")
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
        chronicle_dir().mkdir(parents=True, exist_ok=True)
        log_file = chronicle_dir() / "daemon.log"
        log_fd = open(log_file, "a")
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())
        log_fd.close()

    run_daemon()


if __name__ == "__main__":
    main()
