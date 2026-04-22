"""CLI-level tests for `chronicle uninstall`.

These cover bugs in the rendering/control flow that pure uninstall_hooks
tests can't catch. Each test monkeypatches HOME + sys.argv, calls
uninstall_install() directly, and inspects stdout/stderr.

Why in-process, not subprocess: we need to exercise the module code that
was just modified; a subprocess test would require rebuilding the
PyInstaller binary on every run.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Isolated HOME so uninstall_install's Path.home() and chronicle_dir()
    resolve entirely under tmp_path. Neutralizes real launchd / systemd
    state by forcing service_installed() to report False."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHRONICLE_HOME", raising=False)

    # Reload config so chronicle_dir() re-reads the patched env.
    import chronicle.config
    importlib.reload(chronicle.config)

    # _service.service_installed() checks a module-level path computed at
    # import time against the real HOME — bypass it for uninstall tests.
    monkeypatch.setattr("chronicle.service.service_installed", lambda: False)
    monkeypatch.setattr("chronicle.service.service_file_path", lambda: None)

    return home


def _run(argv, monkeypatch):
    """Invoke uninstall_install() as the dispatcher in __main__.main() would:
    after the 'uninstall' subcommand is consumed, sys.argv[0] becomes
    'chronicle.uninstall' and sys.argv[1:] is the flag list."""
    # Drop the leading "uninstall" token — the dispatcher already consumed it.
    flags = [a for a in argv if a != "uninstall"]
    monkeypatch.setattr(sys, "argv", ["chronicle.uninstall"] + flags)
    from chronicle.__main__ import uninstall_install
    try:
        uninstall_install()
        return 0
    except SystemExit as e:
        return int(e.code or 0)


def test_dry_run_nothing_installed_says_nothing_to_do(fake_home, monkeypatch, capsys):
    """BUG A regression: dry-run on an empty system must NOT render a
    'Remove: (nothing)' placeholder followed by a phantom 'Preserve' list."""
    rc = _run(["uninstall", "--dry-run"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to do" in out
    assert "Remove:" not in out
    assert "Preserve" not in out
    assert "(nothing" not in out


def test_dry_run_leftover_data_only_says_nothing_to_do(fake_home, monkeypatch, capsys):
    """Empty integration + leftover ~/.chronicle/ (no --purge) = nothing to do."""
    ch = fake_home / ".chronicle"
    ch.mkdir()
    (ch / ".processed").mkdir()
    (ch / ".failed").mkdir()
    (ch / "events.jsonl").write_text('{"type":"SessionStart"}\n')

    rc = _run(["uninstall", "--dry-run"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to do" in out
    assert "Preserve" not in out  # no phantom preserve list


def test_dry_run_purge_with_leftover_data_shows_purge_section_only(fake_home, monkeypatch, capsys):
    """--dry-run --purge over leftover-only data: show Purge section, no
    Remove, no Preserve, no Uninstalled."""
    ch = fake_home / ".chronicle"
    ch.mkdir()
    (ch / "events.jsonl").write_text('{}\n')

    rc = _run(["uninstall", "--dry-run", "--purge"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Purge data" in out
    assert str(ch) in out
    assert "Remove integration" not in out
    assert "Preserve" not in out
    assert "Uninstalled" not in out


def test_purge_leftover_data_with_no_install_says_purged_not_uninstalled(fake_home, monkeypatch, capsys):
    """BUG B regression: `--purge --yes` when nothing was installed must
    report 'Purged data' / 'Leftover chronicle data purged', never
    'Uninstalled'."""
    ch = fake_home / ".chronicle"
    ch.mkdir()
    (ch / "events.jsonl").write_text('{}\n')

    rc = _run(["uninstall", "--purge", "--yes"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Purged data" in out
    assert "Uninstalled" not in out  # the specific bug B
    assert "Leftover chronicle data purged" in out
    assert not ch.exists()


def test_execute_nothing_installed_says_nothing_to_do(fake_home, monkeypatch, capsys):
    """Real execution (not --dry-run) with nothing to do: short-circuit, exit 0."""
    rc = _run(["uninstall"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to do" in out
    assert "Preserved" not in out
    assert "Uninstalled" not in out


def test_uninstall_with_hooks_only_strips_hooks(fake_home, monkeypatch, capsys):
    """Only hook entries present (no runtime, no symlinks): integration
    plan is non-empty, uninstall runs, hooks get stripped."""
    import json
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "chronicle-hook"}],
            }],
        },
    }))

    rc = _run(["uninstall"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Uninstalled:" in out
    assert "chronicle-hook entries removed" in out
    remaining = json.loads(settings.read_text())
    assert remaining == {} or remaining == {"hooks": {}}


def test_dry_run_purge_counts_leftover_data_in_plan_size(fake_home, monkeypatch, capsys):
    """Sanity: if BOTH integration (hooks) AND leftover data exist, dry-run
    shows BOTH sections and no phantom (nothing) placeholder."""
    import json
    ch = fake_home / ".chronicle"
    ch.mkdir()
    (ch / "events.jsonl").write_text('{}\n')
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"command": "chronicle-hook"}]}]},
    }))

    rc = _run(["uninstall", "--dry-run", "--purge"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Remove integration:" in out
    assert "Purge data" in out
    assert "chronicle-hook entries" in out
    assert "(nothing" not in out
    assert "Nothing to do" not in out


def test_purge_without_yes_aborts_when_stdin_closed(fake_home, monkeypatch, capsys):
    """Non-interactive context: --purge without --yes must abort, not
    spin trying to read stdin."""
    import io
    ch = fake_home / ".chronicle"
    ch.mkdir()
    (ch / "events.jsonl").write_text('{}\n')
    # Simulate EOF on stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    rc = _run(["uninstall", "--purge"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Aborted" in out
    assert ch.exists()  # untouched because we aborted


def test_purge_confirmation_only_fires_if_there_is_data_to_purge(fake_home, monkeypatch, capsys):
    """--purge with no home_dir present: confirmation prompt should not
    fire. The previous code prompted unconditionally on --purge."""
    import io
    import json
    # Hooks present (so integration plan is non-empty) but NO ~/.chronicle/.
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"matcher": "", "hooks": [{"command": "chronicle-hook"}]}]},
    }))
    # If confirmation fired, EOF on stdin would cause an abort (rc=1).
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    rc = _run(["uninstall", "--purge"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Aborted" not in out
    assert "Uninstalled:" in out


def test_runtime_rmtree_failure_exits_nonzero_without_false_success(fake_home, monkeypatch, capsys):
    """A failed runtime removal must not print a cheerful success summary."""
    import shutil

    runtime_dir = fake_home / ".chronicle" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "chronicle").write_text("binary")

    real_rmtree = shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path) == runtime_dir:
            raise OSError("permission denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    rc = _run(["uninstall"], monkeypatch)
    captured = capsys.readouterr()
    assert rc == 1
    assert "planned uninstall steps did not complete" in captured.err
    assert f"{runtime_dir}/ removed" not in captured.out
    assert "chronicle has been removed" not in captured.out
    assert runtime_dir.exists()
