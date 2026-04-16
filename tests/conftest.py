"""Shared pytest fixtures for Chronicle tests.

Three isolation styles:
- `tmp_home` — isolated HOME only (for tests that don't import chronicle modules
  at the module level OR that monkeypatch module constants directly).
- `chronicle_env` — isolated HOME + reloads chronicle.{config, storage, mode, ...}
  so their module-level `Path.home() / ".chronicle"` constants pick up the
  temp HOME. Use this when tests need to exercise the real marker layer.
- `isolated_env` — builds an env dict suitable for subprocess-based functional
  tests (functional/ dir). PATH contains a fake `claude` stub.

All three use a per-test temp directory and never touch the real
~/.chronicle or ~/.claude.

Shared test helpers:
- `FakeDigest`, `FakeEntry` — minimal duck-typed replacements for
  SessionDigest / ChronicleEntry used across batch + storage tests.
- `seed_session` — factory to write a synthetic Claude Code JSONL.
"""
from __future__ import annotations

import importlib
import os
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE_SRC = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"


# ---------- Fake digest / entry (shared across batch + storage tests) ----------

@dataclass
class FakeDigest:
    """Minimal duck-typed replacement for chronicle.extractor.SessionDigest."""
    session_id: str = ""
    project_slug: str = "-tmp-demo"
    start_time: str = "2026-04-17T00:00:00Z"
    end_time: str = "2026-04-17T00:01:00Z"
    total_turns: int = 1
    user_prompts: list = field(default_factory=list)
    project_path: str = "/tmp/demo"
    git_branch: str = "main"
    timeline: list = field(default_factory=list)
    tool_actions: list = field(default_factory=list)
    assistant_responses: list = field(default_factory=list)

    def __post_init__(self):
        if not self.session_id:
            self.session_id = str(uuid.uuid4())


@dataclass
class FakeEntry:
    """Minimal duck-typed replacement for chronicle.summarizer.ChronicleEntry."""
    session_id: str = ""
    is_error: bool = False
    is_empty: bool = False
    error_kind: str = ""
    error_message: str = ""
    total_cost_usd: float = 0.0
    title: str = "Fake session"
    summary: str = "fake summary"
    narrative: str = ""
    decisions: list = field(default_factory=list)
    problems_solved: list = field(default_factory=list)
    human_reasoning: list = field(default_factory=list)
    follow_ups: list = field(default_factory=list)
    technical_details: dict = field(default_factory=dict)
    architecture: dict = field(default_factory=dict)
    planning: dict = field(default_factory=dict)
    open_questions: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    cross_references: list = field(default_factory=list)
    # fields needed by session_filename():
    start_time: str = "2026-04-17T00:00:00Z"
    # fields needed by entry_to_session_markdown():
    project_path: str = "/tmp/demo"
    git_branch: str = "main"
    total_turns: int = 1
    user_prompts: list = field(default_factory=list)
    tool_actions: list = field(default_factory=list)
    turn_log: str = ""


def _install_fake_claude(bin_dir: Path) -> Path:
    """Install the fake claude binary in bin_dir and return its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "claude"
    target.write_text(FAKE_CLAUDE_SRC.read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return target


# ---------- Fixtures ----------

# Modules whose path constants are computed at import from Path.home().
# Reloading them inside chronicle_env picks up the fake HOME.
_RELOADABLE_MODULES = (
    "chronicle.config",
    "chronicle.mode",
    "chronicle.storage",
    "chronicle.filtering",
    "chronicle.locks",
    "chronicle.service",
    "chronicle.doctor",
    "chronicle.daemon",
    "chronicle.batch",
    "chronicle.query",
    "chronicle.hook",
)


def _reload_chronicle_modules(*names: str) -> None:
    """Reload chronicle submodules so module-level `Path.home()` constants
    re-resolve against the currently active HOME. Safe to call multiple times.
    """
    for name in names:
        if name in sys.modules:
            try:
                importlib.reload(sys.modules[name])
            except ModuleNotFoundError:
                pass
        else:
            importlib.import_module(name)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Per-test isolated HOME. Does NOT reload chronicle modules.
    Use for tests that either don't import chronicle at module level, or
    that monkeypatch module constants directly.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (home / ".chronicle").mkdir(parents=True, exist_ok=True)
    yield home


@pytest.fixture
def chronicle_env(tmp_home):
    """Isolated HOME + reloaded chronicle modules.

    Returns a dict:
      home:             Path to the temp HOME
      chronicle_dir:    ~/.chronicle inside the temp HOME
      claude_projects:  ~/.claude/projects inside the temp HOME
      reload(*modules): re-run importlib.reload after setting a config key

    Use this for any test that calls into chronicle.* functions whose
    behavior depends on filesystem paths under ~/.chronicle or ~/.claude.
    """
    home = tmp_home
    _reload_chronicle_modules(*_RELOADABLE_MODULES)
    # Reset module-level caches that survive reload
    try:
        from chronicle import claude_cli
        claude_cli._reset_cache_for_tests()
    except ImportError:
        pass
    try:
        from chronicle import locks
        locks._reset_daemon_lock_for_tests()
    except ImportError:
        pass

    yield {
        "home": home,
        "chronicle_dir": home / ".chronicle",
        "claude_projects": home / ".claude" / "projects",
        "reload": lambda *mods: _reload_chronicle_modules(*(mods or _RELOADABLE_MODULES)),
    }

    # After the test runs, cleanup caches so they don't leak into the next test
    try:
        from chronicle import claude_cli
        claude_cli._reset_cache_for_tests()
    except ImportError:
        pass
    try:
        from chronicle import locks
        locks._reset_daemon_lock_for_tests()
    except ImportError:
        pass


@pytest.fixture
def fake_claude_bin(tmp_path):
    """Path to a bin dir containing an executable `claude` stub.
    Does not set PATH — caller decides how to inject.
    """
    bin_dir = tmp_path / "bin"
    _install_fake_claude(bin_dir)
    return bin_dir


@pytest.fixture
def isolated_env(tmp_home, fake_claude_bin):
    """Env dict suitable for subprocess spawn in functional tests.

    PATH contains fake_claude_bin + minimal system dirs, mimicking the
    launchd minimal-env scenario but with a resolvable fake `claude`.
    """
    return {
        "HOME": str(tmp_home),
        "PATH": f"{fake_claude_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(REPO_ROOT),
        "FAKE_CLAUDE_MODE": "success",
        "LANG": "C.UTF-8",
    }


@pytest.fixture
def seed_session():
    """Factory to write a synthetic Claude Code session jsonl.

    Returns: seed_session(projects_dir, slug, session_id=None, prompts=[...], cwd="...")
    """
    import json
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
