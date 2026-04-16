"""Shared pytest fixtures for Chronicle tests.

Two isolation styles:
- `tmp_home` monkeypatches HOME for in-process tests (unit/integration)
- `isolated_env` builds a full env dict for subprocess-based tests (functional)

Both use a per-test temp directory and never touch the real ~/.chronicle
or ~/.claude.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE_SRC = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"


def _install_fake_claude(bin_dir: Path, mode: str = "success") -> Path:
    """Install the fake claude binary in bin_dir and return its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "claude"
    target.write_text(FAKE_CLAUDE_SRC.read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Per-test isolated HOME. Chronicle modules read Path.home()-based
    paths; tests that import chronicle modules must do so AFTER this
    fixture activates, or use `monkeypatch.setattr` to override module
    attributes directly.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    # Clear any real anthropic creds so tests never accidentally hit the
    # real API even if a bug leaks env through
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (home / ".chronicle").mkdir(parents=True, exist_ok=True)
    yield home


@pytest.fixture
def fake_claude_bin(tmp_path):
    """Path to a bin dir containing an executable `claude` stub.
    Does not set PATH — caller decides how to inject.
    """
    bin_dir = tmp_path / "bin"
    _install_fake_claude(bin_dir)
    return bin_dir


@pytest.fixture
def isolated_env(tmp_home, fake_claude_bin, monkeypatch):
    """Env dict suitable for subprocess spawn in functional tests.

    PATH contains fake_claude_bin + minimal system dirs, mimicking the
    launchd minimal-env scenario but with a resolvable fake `claude`.
    """
    env = {
        "HOME": str(tmp_home),
        "PATH": f"{fake_claude_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(REPO_ROOT),
        "FAKE_CLAUDE_MODE": "success",
        "LANG": "C.UTF-8",
    }
    return env


@pytest.fixture
def seed_session():
    """Factory to write a synthetic Claude Code session jsonl into a project dir.

    Returns a function: seed_session(projects_dir, slug, session_id, prompts=[...]).
    """
    import json
    import uuid
    from datetime import datetime, timezone

    def _seed(projects_dir: Path, slug: str, session_id: str | None = None,
              prompts: list[str] | None = None, cwd: str = "/tmp/fake") -> Path:
        session_id = session_id or str(uuid.uuid4())
        prompts = prompts or ["test prompt"]
        proj = projects_dir / slug
        proj.mkdir(parents=True, exist_ok=True)
        jsonl = proj / f"{session_id}.jsonl"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = []
        for i, p in enumerate(prompts):
            lines.append(json.dumps({
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "sessionId": session_id,
                "timestamp": now,
                "cwd": cwd,
                "gitBranch": "main",
                "message": {"content": p},
            }))
            lines.append(json.dumps({
                "type": "assistant",
                "uuid": str(uuid.uuid4()),
                "sessionId": session_id,
                "timestamp": now,
                "message": {
                    "content": [{"type": "text", "text": f"response {i}"}]
                },
            }))
        jsonl.write_text("\n".join(lines) + "\n")
        return jsonl

    return _seed
