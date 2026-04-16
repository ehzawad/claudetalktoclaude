"""Hook dispatcher for Claude Code events.

Handles four events:
- SessionStart (sync): auto-spawns daemon, injects recent decisions as additionalContext
- Stop (async): logs event for daemon processing
- UserPromptSubmit (async): logs event, resets daemon's global debounce timer
- SessionEnd (async): logs event for daemon processing

All async hooks just append to events.jsonl and exit. They never return
decisions, never block the session, never modify behavior.
"""

import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import CHRONICLE_DIR, PID_FILE, EVENTS_FILE, load_recent_titles

_MAX_ERROR_LOG_BYTES = 1_000_000  # ~1MB cap

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _daemon_running() -> bool:
    """Check if the daemon process is alive via PID file."""
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def _spawn_daemon():
    """Launch daemon in background, fully detached from this process."""
    log_file = CHRONICLE_DIR / "daemon.log"
    with open(log_file, "a") as log_fd:
        subprocess.Popen(
            [sys.executable, "-m", "chronicle.daemon"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(PROJECT_ROOT),
        )


def main():
    try:
        CHRONICLE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(str(CHRONICLE_DIR), 0o700)
        data = json.loads(sys.stdin.read())
        event_name = data.get("hook_event_name", "")
        data["chronicle_timestamp"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Always log the event
        with open(EVENTS_FILE, "a") as f:
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

            # Inject recent decisions into the session
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
            error_log = CHRONICLE_DIR / "hook-errors.log"
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
