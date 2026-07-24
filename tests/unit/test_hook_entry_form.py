"""Exec-form hook installation and the two data-loss regressions it can cause.

Claude Code runs a command hook in exec form when `args` is present and shell
form when it is omitted (official hooks reference). Chronicle only ever writes
and only ever recognizes exec form: an entry without `args` is somebody else's,
including the shell-form entry Chronicle itself wrote through v0.12.3. Both
regressions below are a direct consequence of getting that distinction wrong.
"""
from __future__ import annotations

import json

import pytest

from chronicle.install_hooks import (
    _chronicle_hook_path,
    _is_chronicle_hook_entry,
    install_hooks,
    uninstall_hooks,
)


def _chronicle_entries(data: dict, event: str) -> list[dict]:
    return [
        hook
        for group in data["hooks"][event]
        for hook in group.get("hooks", [])
        if _is_chronicle_hook_entry(hook)
    ]


EVENTS = ("SessionStart", "Stop", "UserPromptSubmit", "SessionEnd")


# ---------- exec form ----------

def test_installs_absolute_exec_form_hooks(tmp_path, monkeypatch):
    """Hooks must not depend on a non-interactive shell's PATH or profiles."""
    home = tmp_path / "home with spaces"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHRONICLE_HOOK_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    settings = home / ".claude" / "settings.json"

    install_hooks(str(settings))
    data = json.loads(settings.read_text())
    expected = str(home / ".local" / "bin" / "chronicle-hook")

    for event in EVENTS:
        entries = _chronicle_entries(data, event)
        assert len(entries) == 1
        assert entries[0]["command"] == expected
        # Presence of `args` — not its contents — selects exec form.
        assert entries[0]["args"] == []
        # Omitted matcher means "match all" for these lifecycle events.
        assert "matcher" not in data["hooks"][event][-1]


# ---------- regression 1: unrelated user hooks must survive ----------

@pytest.mark.parametrize("user_command", [
    "my-tool --config /opt/chronicle-hook",
    "echo done >> /var/log/chronicle-hook",
    "notify-send /opt/tools/chronicle-hook",
])
def test_user_hook_merely_mentioning_the_name_is_not_ours(tmp_path, user_command):
    """The whole command string is one executable path, and an entry without
    `args` is never ours. Basenaming these classified them as Chronicle's and
    silently deleted them, violating the preservation guarantee in README."""
    assert not _is_chronicle_hook_entry({"type": "command", "command": user_command})

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": user_command}]}
    ]}}))

    install_hooks(str(settings))
    data = json.loads(settings.read_text())
    surviving = [h.get("command") for g in data["hooks"]["Stop"] for h in g.get("hooks", [])]
    assert user_command in surviving

    # Uninstall must remove only Chronicle's own four entries.
    assert uninstall_hooks(str(settings)) == len(EVENTS)
    data = json.loads(settings.read_text())
    surviving = [h.get("command") for g in data["hooks"]["Stop"] for h in g.get("hooks", [])]
    assert surviving == [user_command]


def test_exec_form_path_with_spaces_is_recognized():
    """Exec form is a single executable path, so it must NOT be tokenized."""
    entry = {"type": "command",
             "command": "/home/First Last/.local/bin/chronicle-hook",
             "args": []}
    assert _is_chronicle_hook_entry(entry)
    # Same string WITHOUT args is not exec form, so it is not ours.
    assert not _is_chronicle_hook_entry({"type": "command", "command": entry["command"]})


def test_pre_v0_12_4_shell_form_entries_are_not_recognized():
    """Shell form is no longer Chronicle's, including the entry Chronicle
    itself wrote through v0.12.3. Only an `args` key makes an entry ours."""
    for cmd in ("chronicle-hook", "chronicle-hook --foo",
                "/Users/ehz/.local/bin/chronicle-hook"):
        assert not _is_chronicle_hook_entry({"type": "command", "command": cmd}), cmd


def test_upgrade_from_v0_12_3_duplicates_instead_of_migrating(tmp_path, monkeypatch):
    """Accepted consequence of dropping shell-form recognition: the pre-v0.12.4
    entry is neither rewritten nor removed, so the hook fires twice per event
    until the user hand-edits settings.json."""
    monkeypatch.setenv("CHRONICLE_HOOK_PATH", str(tmp_path / "bin" / "chronicle-hook"))
    settings = tmp_path / "settings.json"
    old_entry = {"type": "command", "command": "chronicle-hook"}
    settings.write_text(json.dumps({"hooks": {ev: [
        {"matcher": "", "hooks": [dict(old_entry)]}
    ] for ev in EVENTS}}))

    install_hooks(str(settings))
    install_hooks(str(settings))  # idempotent for OUR entries
    data = json.loads(settings.read_text())
    for event in EVENTS:
        # Exactly one entry Chronicle owns...
        entries = _chronicle_entries(data, event)
        assert len(entries) == 1
        assert entries[0]["args"] == []
        assert entries[0]["command"] == str(tmp_path / "bin" / "chronicle-hook")
        # ...alongside the untouched old one, in its own surviving group.
        assert len(data["hooks"][event]) == 2
        assert data["hooks"][event][0]["hooks"] == [old_entry]

    # Uninstall strips only the four exec-form entries; the old ones remain.
    assert uninstall_hooks(str(settings)) == len(EVENTS)
    data = json.loads(settings.read_text())
    for event in EVENTS:
        assert data["hooks"][event] == [{"matcher": "", "hooks": [old_entry]}]


# ---------- regression 2: console-script installs must not get a dead path ----------

def test_console_script_install_is_resolved_via_path(tmp_path, monkeypatch):
    """pip / uv / venv / Homebrew put chronicle-hook outside ~/.local/bin.

    Hardcoding ~/.local/bin wrote a nonexistent path; exec form has no shell
    and therefore no PATH fallback, so every hook silently failed.
    """
    home = tmp_path / "home"
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    hook = venv_bin / "chronicle-hook"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHRONICLE_HOOK_PATH", raising=False)
    monkeypatch.setenv("PATH", str(venv_bin))

    resolved = _chronicle_hook_path()
    assert resolved == hook
    assert resolved.exists()


def test_installer_supplied_path_wins_even_before_it_exists(tmp_path, monkeypatch):
    """install.sh configures hooks BEFORE creating the symlink, so the
    installer-supplied path must be trusted over anything discoverable."""
    planned = tmp_path / "local" / "bin" / "chronicle-hook"
    monkeypatch.setenv("CHRONICLE_HOOK_PATH", str(planned))
    monkeypatch.setattr("shutil.which", lambda name: "/somewhere/else/chronicle-hook")

    assert _chronicle_hook_path() == planned
    assert not planned.exists()


def test_warns_when_resolved_hook_path_is_missing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHRONICLE_HOOK_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    install_hooks(str(home / ".claude" / "settings.json"))
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "CHRONICLE_HOOK_PATH" in err
