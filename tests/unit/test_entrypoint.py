"""Unit tests for the PyInstaller busybox entrypoint."""

from __future__ import annotations

import sys
import types

import pytest


def test_entrypoint_dispatches_hook_by_argv0(monkeypatch):
    import chronicle._entrypoint as entrypoint

    hook_mod = types.ModuleType("chronicle.hook")
    hook_mod.main = lambda: 17
    cli_mod = types.ModuleType("chronicle.__main__")
    cli_mod.main = lambda: 0

    monkeypatch.setitem(sys.modules, "chronicle.hook", hook_mod)
    monkeypatch.setitem(sys.modules, "chronicle.__main__", cli_mod)
    monkeypatch.setattr(sys, "argv", ["/tmp/chronicle-hook"])

    with pytest.raises(SystemExit) as excinfo:
        entrypoint.main()
    assert excinfo.value.code == 17


def test_entrypoint_dispatches_cli_for_unknown_names(monkeypatch):
    import chronicle._entrypoint as entrypoint

    hook_mod = types.ModuleType("chronicle.hook")
    hook_mod.main = lambda: 99
    cli_mod = types.ModuleType("chronicle.__main__")
    cli_mod.main = lambda: 23

    monkeypatch.setitem(sys.modules, "chronicle.hook", hook_mod)
    monkeypatch.setitem(sys.modules, "chronicle.__main__", cli_mod)
    monkeypatch.setattr(sys, "argv", ["/tmp/codex-chronicle"])

    with pytest.raises(SystemExit) as excinfo:
        entrypoint.main()
    assert excinfo.value.code == 23
