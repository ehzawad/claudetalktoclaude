"""Unit tests for chronicle.install_hooks.uninstall_hooks().

The uninstall path MUST be subtractive at the hook-entry level (not the
matcher-group level) so a user who added their own hook into a matcher
group alongside chronicle's doesn't lose it when they run
`chronicle uninstall`.
"""
from __future__ import annotations

import json
from pathlib import Path


def test_uninstall_on_missing_file_returns_zero(tmp_path):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    assert uninstall_hooks(str(settings)) == 0


def test_uninstall_on_file_without_hooks_returns_zero(tmp_path):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark"}))
    assert uninstall_hooks(str(settings)) == 0
    # File content is untouched
    assert json.loads(settings.read_text()) == {"theme": "dark"}


def test_uninstall_removes_only_chronicle_entries(tmp_path):
    """A matcher group with both chronicle and user hooks keeps the user's."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": "chronicle-hook"},
                    {"type": "command", "command": "my-custom-logger"},
                ],
            }],
        },
    }
    settings.write_text(json.dumps(data))
    removed = uninstall_hooks(str(settings))
    assert removed == 1
    result = json.loads(settings.read_text())
    assert result == {
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "my-custom-logger"}],
            }],
        },
    }


def test_uninstall_drops_empty_matcher_groups(tmp_path):
    """If a matcher group contains ONLY chronicle-hook, drop the whole group."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook"}]},
                {"matcher": "other", "hooks": [{"type": "command", "command": "user-hook"}]},
            ],
        },
    }
    settings.write_text(json.dumps(data))
    removed = uninstall_hooks(str(settings))
    assert removed == 1
    result = json.loads(settings.read_text())
    # The chronicle-only group is gone; the user's is intact.
    assert result["hooks"]["SessionStart"] == [
        {"matcher": "other", "hooks": [{"type": "command", "command": "user-hook"}]},
    ]


def test_uninstall_drops_empty_events(tmp_path):
    """If an event's matcher groups all become empty, drop the event."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "theme": "dark",
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"command": "chronicle-hook"}]},
            ],
            "Stop": [
                {"matcher": "", "hooks": [{"command": "chronicle-hook", "async": True}]},
            ],
        },
    }
    settings.write_text(json.dumps(data))
    removed = uninstall_hooks(str(settings))
    assert removed == 2
    result = json.loads(settings.read_text())
    # The whole "hooks" key disappears because both events became empty.
    assert "hooks" not in result
    assert result.get("theme") == "dark"


def test_uninstall_matches_absolute_path_commands(tmp_path):
    """chronicle-hook can be invoked by absolute path (e.g. /Users/x/.local/bin/chronicle-hook)."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [
                    {"command": "/Users/ehz/.local/bin/chronicle-hook"},
                    {"command": "/usr/local/bin/other-tool"},
                ],
            }],
        },
    }
    settings.write_text(json.dumps(data))
    removed = uninstall_hooks(str(settings))
    assert removed == 1
    result = json.loads(settings.read_text())
    assert result["hooks"]["SessionStart"][0]["hooks"] == [
        {"command": "/usr/local/bin/other-tool"},
    ]


def test_uninstall_matches_command_with_flags(tmp_path):
    """'chronicle-hook --something' still resolves to chronicle-hook basename."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"command": "chronicle-hook --verbose"}]}],
        },
    }
    settings.write_text(json.dumps(data))
    assert uninstall_hooks(str(settings)) == 1


def test_uninstall_dry_run_does_not_write(tmp_path):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"command": "chronicle-hook"}]}],
        },
    }
    raw = json.dumps(data)
    settings.write_text(raw)
    removed = uninstall_hooks(str(settings), dry_run=True)
    assert removed == 1
    # File is byte-for-byte unchanged.
    assert settings.read_text() == raw


def test_uninstall_malformed_json_leaves_file_alone(tmp_path, capsys):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    settings.write_text("{not valid json")
    assert uninstall_hooks(str(settings)) == 0
    # File is unchanged.
    assert settings.read_text() == "{not valid json"
    # A warning went to stderr.
    err = capsys.readouterr().err
    assert "WARN" in err


def test_uninstall_top_level_not_an_object_is_safe(tmp_path, capsys):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(["this", "is", "an", "array"]))
    assert uninstall_hooks(str(settings)) == 0
    assert json.loads(settings.read_text()) == ["this", "is", "an", "array"]


def test_uninstall_then_install_is_idempotent(tmp_path):
    """Uninstall -> install reaches the same state as install-from-scratch."""
    from chronicle.install_hooks import install_hooks, uninstall_hooks
    settings = tmp_path / "settings.json"
    install_hooks(str(settings))
    state_after_install = json.loads(settings.read_text())
    uninstall_hooks(str(settings))
    install_hooks(str(settings))
    assert json.loads(settings.read_text()) == state_after_install


def test_is_chronicle_hook_command_variants():
    from chronicle.install_hooks import _is_chronicle_hook_command as f
    assert f("chronicle-hook") is True
    assert f("/Users/ehz/.local/bin/chronicle-hook") is True
    assert f("chronicle-hook --flag") is True
    assert f("  chronicle-hook  ") is True
    assert f("chronicle") is False  # the CLI, not the hook
    assert f("fake-chronicle-hook") is False
    assert f("") is False
    assert f(None) is False
    assert f(42) is False
