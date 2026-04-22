"""Hook dispatcher for Claude Code events.

Behavior is identical in foreground and background modes EXCEPT for the
daemon-spawn step on SessionStart:

- SessionStart (sync): always appends event, always injects past session
  titles as `additionalContext`. In background mode only, respawns the
  daemon if it's dead. In foreground mode, never spawns the daemon.
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
    chronicle_dir, pid_file, events_file, load_recent_titles,
)

_MAX_ERROR_LOG_BYTES = 1_000_000  # ~1MB cap

def _daemon_running() -> bool:
    """Check if the daemon process is alive via PID file."""
    try:
        pid = int(pid_file().read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
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
        chronicle_dir().mkdir(parents=True, exist_ok=True)
        os.chmod(str(chronicle_dir()), 0o700)
        data = json.loads(sys.stdin.read())
        event_name = data.get("hook_event_name", "")
        data["chronicle_timestamp"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Always log the event
        with open(events_file(), "a") as f:
            f.write(json.dumps(data, separators=(",", ":")) + "\n")

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
            if cwd:
                slug = cwd.replace("/", "-")
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
        except Exception:
            pass  # truly last resort — cannot even log


if __name__ == "__main__":
    main()
