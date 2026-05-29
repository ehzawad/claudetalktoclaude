"""Hook dispatcher for Claude Code events.

Foreground and background modes both log configured hook events. They differ
on SessionStart daemon spawning, and foreground mode also caps events.jsonl
because no daemon consumes it there:

- SessionStart (sync): always appends event, emits past session titles as
  `additionalContext` only when titles exist. In background mode only,
  respawns the daemon if it's dead. In foreground mode, never spawns the daemon.
- UserPromptSubmit / Stop / SessionEnd (async): always append to
  events.jsonl and exit. Never return decisions, never block.

Hooks never call `claude -p` — summarization is gated behind explicit
user commands or the (opt-in) background daemon.
"""

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    chronicle_dir, events_file, load_recent_titles,
    project_slug_for,
)

_MAX_ERROR_LOG_BYTES = 1_000_000  # ~1MB cap
_MAX_EVENTS_BYTES = 5 * 1024 * 1024  # ~5 MiB foreground cap for events.jsonl


def _lock_file_exclusive(f):
    """Best-effort advisory lock around event-file append/truncate operations."""
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    except Exception:
        return False
    return True


def _unlock_file(f):
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def _chmod_owner_only(path: Path):
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _cap_events_foreground():
    """events.jsonl is consumed only by the background daemon's offset reader;
    in foreground mode nothing reads it, so truncating the whole file when it
    exceeds the cap is safe and prevents unbounded growth (BUG-08). Never raises
    — the hook must not block the session. Background compaction is handled by
    the daemon once its reader has consumed the file."""
    try:
        from .mode import is_foreground_mode
        ef = events_file()
        if is_foreground_mode() and ef.exists() and ef.stat().st_size > _MAX_EVENTS_BYTES:
            with open(ef, "r+b") as f:
                locked = _lock_file_exclusive(f)
                try:
                    f.truncate(0)
                finally:
                    if locked:
                        _unlock_file(f)
            _chmod_owner_only(ef)
    except Exception:
        pass


def _daemon_running() -> bool:
    """Check if the daemon is alive via the flock-authoritative probe (BUG-10):
    the OS releases daemon.pid's lock on death, so a recycled PID can't be
    mistaken for a live daemon and wrongly suppress the SessionStart respawn."""
    try:
        from .locks import daemon_is_running
        running, _pid = daemon_is_running()
        return running
    except Exception:
        return False


def _spawn_daemon_cmd() -> list[str]:
    """argv for respawning the daemon.

    In a PyInstaller-frozen build, `sys.executable` is the `chronicle`
    binary; the bootloader ignores `-m`, so we have to use the CLI's
    `daemon` subcommand instead. In dev (non-frozen), `python -m
    chronicle.daemon` runs the module directly.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]
    return [sys.executable, "-m", "chronicle.daemon"]


def _spawn_daemon():
    """Launch daemon in background, fully detached from this process."""
    log_file = chronicle_dir() / "daemon.log"
    with open(log_file, "a") as log_fd:
        subprocess.Popen(
            _spawn_daemon_cmd(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(Path.home()),
        )


def main():
    try:
        os.umask(0o077)  # owner-only perms for events.jsonl etc. (BUG-25)
        chronicle_dir().mkdir(parents=True, exist_ok=True)
        os.chmod(str(chronicle_dir()), 0o700)
        data = json.loads(sys.stdin.read())
        event_name = data.get("hook_event_name", "")
        data["chronicle_timestamp"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Always log the event
        with open(events_file(), "a") as f:
            locked = _lock_file_exclusive(f)
            try:
                f.write(json.dumps(data, separators=(",", ":")) + "\n")
                # Flush the append while still holding the lock so the daemon's
                # compaction (which takes the same lock) can never fstat a stale
                # size and truncate away an event mid-flush (BUG-08).
                f.flush()
            finally:
                if locked:
                    _unlock_file(f)
        _chmod_owner_only(events_file())
        _cap_events_foreground()

        if event_name == "SessionStart":
            # Only respawn the daemon if we're in background mode. In
            # foreground mode the user opted out of passive processing.
            try:
                from .mode import is_background_mode
                bg = is_background_mode()
            except Exception:
                bg = False
            if bg and not _daemon_running():
                _spawn_daemon()

            # Inject recent session titles as additionalContext — the user's
            # session sees "Previous sessions: …" without any tokens being
            # spent on Chronicle's side.
            cwd = data.get("cwd", "")
            tp = data.get("transcript_path", "")
            if cwd or tp:
                slug = project_slug_for(cwd, tp or None)
                titles = load_recent_titles(slug)
                if titles:
                    context = (
                        "Previous sessions in this project (from Decision Chronicle):\n"
                        + "\n".join(f"- {t}" for t in titles)
                        + "\n\nThese are chronicled decisions from past sessions. "
                        "You can reference them if relevant to the current work."
                    )
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": context,
                        }
                    }))

    except Exception:
        # Never block the primary session, but log failures for diagnosis.
        try:
            error_log = chronicle_dir() / "hook-errors.log"
            error_log.parent.mkdir(parents=True, exist_ok=True)
            # Cap log size by truncating from the front
            if error_log.exists() and error_log.stat().st_size > _MAX_ERROR_LOG_BYTES:
                content = error_log.read_bytes()
                error_log.write_bytes(content[-(_MAX_ERROR_LOG_BYTES // 2):])
            with open(error_log, "a") as f:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(f"\n--- {ts} ---\n{traceback.format_exc()}")
            _chmod_owner_only(error_log)
        except Exception:
            pass  # truly last resort — cannot even log


if __name__ == "__main__":
    main()
