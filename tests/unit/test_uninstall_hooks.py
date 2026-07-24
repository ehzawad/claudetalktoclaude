"""Unit tests for chronicle install/uninstall hook merging.

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
                    {"type": "command", "command": "chronicle-hook", "args": []},
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
                {"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "args": []}]},
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
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "chronicle-hook", "args": []}]},
            ],
            "Stop": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "chronicle-hook", "args": [], "async": True}]},
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


def test_uninstall_matches_exec_form_absolute_paths(tmp_path):
    """chronicle-hook is normally installed by absolute path (e.g. /Users/x/.local/bin/chronicle-hook)."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [
                    {"type": "command",
                     "command": "/Users/ehz/.local/bin/chronicle-hook", "args": []},
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
    """`args` contents are irrelevant — presence alone selects exec form."""
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": "chronicle-hook", "args": ["--verbose"]}]}],
        },
    }
    settings.write_text(json.dumps(data))
    assert uninstall_hooks(str(settings)) == 1


def test_uninstall_dry_run_does_not_write(tmp_path):
    from chronicle.install_hooks import uninstall_hooks
    settings = tmp_path / "settings.json"
    data = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": "chronicle-hook", "args": []}]}],
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


def test_is_chronicle_hook_entry_variants():
    """Only exec form — an entry with an `args` key — is Chronicle's."""
    from chronicle.install_hooks import _is_chronicle_hook_entry as f

    def cmd(command, **extra):
        return {"type": "command", "command": command, **extra}

    # Exec form (what Chronicle writes today) — whole string is the executable.
    assert f(cmd("/Users/ehz/.local/bin/chronicle-hook", args=[])) is True
    assert f(cmd("/home/First Last/.local/bin/chronicle-hook", args=[])) is True
    assert f(cmd("chronicle-hook", args=[])) is True
    assert f(cmd("  chronicle-hook  ", args=[])) is True
    assert f(cmd("chronicle-hook", args=["--verbose"])) is True

    # No `args` key at all is not an entry Chronicle writes.
    assert f(cmd("chronicle-hook")) is False
    assert f(cmd("/Users/ehz/.local/bin/chronicle-hook")) is False
    assert f({"command": "chronicle-hook"}) is False

    # A missing `type` is tolerated — Claude Code defaults it to "command".
    # Defensive parsing of hand-edited settings, not backwards compatibility:
    # rejecting these would strand them in install, uninstall AND doctor.
    assert f({"command": "chronicle-hook", "args": []}) is True
    assert f({"command": "/Users/ehz/.local/bin/chronicle-hook", "args": []}) is True

    # `command` is one whole executable path, never shell-tokenized.
    assert f(cmd("chronicle-hook --flag", args=[])) is False
    # An unrelated command that merely MENTIONS the name is not ours.
    assert f(cmd("my-tool --config /opt/chronicle-hook")) is False
    assert f(cmd("echo done >> /var/log/chronicle-hook")) is False
    assert f(cmd("chronicle", args=[])) is False  # the CLI, not the hook
    assert f(cmd("fake-chronicle-hook", args=[])) is False

    # Malformed / non-command entries.
    assert f(cmd("", args=[])) is False
    assert f(cmd(None, args=[])) is False
    assert f(cmd(42, args=[])) is False
    assert f({"type": "prompt", "command": "chronicle-hook", "args": []}) is False
    assert f(None) is False
