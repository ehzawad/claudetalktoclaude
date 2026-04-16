"""Paths, constants, and configuration for the Decision Chronicle system."""

import json
import os
from pathlib import Path

CHRONICLE_DIR = Path.home() / ".chronicle"
EVENTS_FILE = CHRONICLE_DIR / "events.jsonl"
OFFSET_FILE = CHRONICLE_DIR / "events.offset"
PID_FILE = CHRONICLE_DIR / "daemon.pid"
PROCESSING_LOCK = CHRONICLE_DIR / "processing.lock"
CONFIG_FILE = CHRONICLE_DIR / "config.json"
PROJECTS_DIR = CHRONICLE_DIR / "projects"
PROCESSED_DIR = CHRONICLE_DIR / ".processed"
FAILED_DIR = CHRONICLE_DIR / ".failed"

# Claude Code's session transcript storage — the source of truth chronicle reads from.
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Processing modes:
#   "foreground" — no daemon; user runs `chronicle process` on demand. Default.
#   "background" — daemon auto-summarizes via launchd/systemd.
PROCESSING_MODES = ("foreground", "background")

DEFAULT_CONFIG = {
    "processing_mode": "foreground",
    "concurrency": 5,
    "model": "opus",
    "poll_interval_seconds": 5,
    "quiet_minutes": 5,
    "scan_interval_minutes": 30,
    "max_retries": 3,
    "skip_projects": [],
    "fallback_model": "sonnet",
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            user_config = json.load(f)
        return {**DEFAULT_CONFIG, **user_config}
    return dict(DEFAULT_CONFIG)


def save_default_config():
    CHRONICLE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CHRONICLE_DIR, 0o700)
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        os.chmod(CONFIG_FILE, 0o600)


def project_chronicle_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug


def ensure_dirs(slug: str):
    d = project_chronicle_dir(slug)
    created = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(d, 0o700)
    sessions_dir = d / "sessions"
    sessions_created = not sessions_dir.exists()
    sessions_dir.mkdir(exist_ok=True)
    if sessions_created:
        os.chmod(sessions_dir, 0o700)


def load_recent_titles(project_slug: str, max_entries: int = 10) -> list[str]:
    """Read recent session titles from a project's chronicle sessions dir."""
    sessions_dir = PROJECTS_DIR / project_slug / "sessions"
    if not sessions_dir.exists():
        return []

    titles = []
    for md_file in sorted(sessions_dir.glob("*.md"), reverse=True)[:max_entries]:
        try:
            with open(md_file, errors="ignore") as f:
                first_line = f.readline().rstrip("\n")
            if first_line.startswith("# "):
                titles.append(first_line[2:])
        except Exception:
            continue
    return titles
