"""Paths, constants, and configuration for the Decision Chronicle system."""

import json
import os
from pathlib import Path

CHRONICLE_DIR = Path.home() / ".chronicle"
EVENTS_FILE = CHRONICLE_DIR / "events.jsonl"
OFFSET_FILE = CHRONICLE_DIR / "events.offset"
PID_FILE = CHRONICLE_DIR / "daemon.pid"
CONFIG_FILE = CHRONICLE_DIR / "config.json"
PROJECTS_DIR = CHRONICLE_DIR / "projects"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

DEFAULT_CONFIG = {
    "concurrency": 5,
    "min_turns_to_chronicle": 1,
    "model": "opus",
    "poll_interval_seconds": 5,
    "quiet_minutes": 5,
    "max_retries": 3,
    "skip_projects": [],
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
