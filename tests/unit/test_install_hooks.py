"""Unit tests for chronicle.install_hooks.

Chronicle must NOT silently clobber a user's ~/.claude/settings.json if
it's malformed — it should refuse with a clear error message so the
user can fix (or back up) the file themselves.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_creates_fresh_settings_when_absent(tmp_path):
    from chronicle.install_hooks import install_hooks
    settings = tmp_path / "settings.json"
    install_hooks(str(settings))
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert "hooks" in data
    assert "SessionStart" in data["hooks"]


def test_merges_into_existing_valid_settings(tmp_path):
    from chronicle.install_hooks import install_hooks
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "theme": "dark",
        "hooks": {"MyEvent": [{"matcher": "", "hooks": []}]},
    }))
    install_hooks(str(settings))
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"  # user's key survives
    assert "MyEvent" in data["hooks"]  # user's hooks survive
    assert "SessionStart" in data["hooks"]  # chronicle hooks added


def test_malformed_json_refuses_with_exit_code(tmp_path, capsys):
    from chronicle.install_hooks import install_hooks
    settings = tmp_path / "settings.json"
    settings.write_text("{ not json,}")  # invalid
    with pytest.raises(SystemExit) as excinfo:
        install_hooks(str(settings))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    # File is left UNCHANGED — we don't clobber user state
    assert settings.read_text() == "{ not json,}"


def test_non_object_json_refuses(tmp_path, capsys):
    from chronicle.install_hooks import install_hooks
    settings = tmp_path / "settings.json"
    settings.write_text('"just a string"')
    with pytest.raises(SystemExit) as excinfo:
        install_hooks(str(settings))
    assert excinfo.value.code == 2


def test_idempotent_reinstall_doesnt_duplicate_hooks(tmp_path):
    from chronicle.install_hooks import install_hooks
    settings = tmp_path / "settings.json"
    install_hooks(str(settings))
    install_hooks(str(settings))  # run again
    data = json.loads(settings.read_text())
    for event in ("SessionStart", "Stop", "UserPromptSubmit", "SessionEnd"):
        # Each event should have exactly ONE chronicle-hook matcher group
        groups = data["hooks"][event]
        chronicle_count = sum(
            1 for g in groups
            for h in g.get("hooks", [])
            if h.get("command") == "chronicle-hook"
        )
        assert chronicle_count == 1, (
            f"{event}: expected 1 chronicle-hook, got {chronicle_count}"
        )
