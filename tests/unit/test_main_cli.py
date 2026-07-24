"""Unit tests for chronicle.__main__ command helpers."""

from __future__ import annotations

import sys
import types

import pytest


def test_update_install_exits_with_remote_installer_status(monkeypatch):
    from chronicle import __main__ as main_mod

    monkeypatch.setattr(main_mod, "_installer_url", lambda: "https://example.test/install.sh")
    monkeypatch.setattr(main_mod, "_run_remote_install_script", lambda url: 7)

    with pytest.raises(SystemExit) as excinfo:
        main_mod.update_install()
    assert excinfo.value.code == 7


def test_removed_reload_command_is_rejected(monkeypatch, capsys):
    """`reload` was a pre-binary-build alias for `update`; it is gone."""
    from chronicle import __main__ as main_mod

    monkeypatch.setattr(sys, "argv", ["chronicle", "reload"])

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code == 1
    assert "Unknown command: reload" in capsys.readouterr().out


def test_install_daemon_surfaces_service_error(monkeypatch, capsys):
    from chronicle import __main__ as main_mod
    import chronicle.service as service_mod

    fake_service = types.ModuleType("chronicle.service")
    fake_service._MAC_PLIST_PATH = "/tmp/fake.plist"
    fake_service._LINUX_UNIT_PATH = "/tmp/fake.service"
    def _raise():
        raise RuntimeError("launchctl bootstrap failed: permission denied")
    fake_service.install_service = _raise
    fake_service.service_installed = lambda: False
    fake_service.service_file_path = lambda: None
    fake_service.last_service_error = lambda: "launchctl bootstrap failed: permission denied"

    monkeypatch.setitem(sys.modules, "chronicle.service", fake_service)
    monkeypatch.setattr(service_mod, "install_service", fake_service.install_service)
    monkeypatch.setattr(service_mod, "last_service_error", fake_service.last_service_error)
    monkeypatch.setattr(service_mod, "service_installed", fake_service.service_installed)
    monkeypatch.setattr(service_mod, "service_file_path", fake_service.service_file_path)
    monkeypatch.setattr(service_mod, "_MAC_PLIST_PATH", fake_service._MAC_PLIST_PATH)
    monkeypatch.setattr(service_mod, "_LINUX_UNIT_PATH", fake_service._LINUX_UNIT_PATH)
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr("chronicle.mode.set_processing_mode", lambda mode: None)

    with pytest.raises(SystemExit) as excinfo:
        main_mod.install_daemon()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "permission denied" in err


def test_install_daemon_rolls_mode_back_on_service_failure(monkeypatch):
    # BUG-16: when the service manager rejects the job (accepted=False),
    # install_daemon must roll processing_mode back to foreground (Option B),
    # not leave it stuck at background with no running daemon.
    from chronicle import __main__ as main_mod
    import chronicle.service as service_mod

    fake_service = types.ModuleType("chronicle.service")
    fake_service._MAC_PLIST_PATH = "/tmp/fake.plist"
    fake_service._LINUX_UNIT_PATH = "/tmp/fake.service"
    def _raise():
        raise RuntimeError("launchctl bootstrap failed")
    fake_service.install_service = _raise
    fake_service.service_installed = lambda: False
    fake_service.service_file_path = lambda: None
    fake_service.last_service_error = lambda: "launchctl bootstrap failed"
    monkeypatch.setitem(sys.modules, "chronicle.service", fake_service)
    monkeypatch.setattr(service_mod, "install_service", fake_service.install_service)
    monkeypatch.setattr(service_mod, "last_service_error", fake_service.last_service_error)
    monkeypatch.setattr(service_mod, "service_installed", fake_service.service_installed)
    monkeypatch.setattr(service_mod, "service_file_path", fake_service.service_file_path)
    monkeypatch.setattr(service_mod, "_MAC_PLIST_PATH", fake_service._MAC_PLIST_PATH)
    monkeypatch.setattr(service_mod, "_LINUX_UNIT_PATH", fake_service._LINUX_UNIT_PATH)
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")

    calls = []
    monkeypatch.setattr("chronicle.mode.set_processing_mode", lambda mode: calls.append(mode))
    with pytest.raises(SystemExit) as excinfo:
        main_mod.install_daemon()
    assert excinfo.value.code == 1
    # background set first, then rolled back to foreground on the failure branch.
    assert calls == ["background", "foreground"]
