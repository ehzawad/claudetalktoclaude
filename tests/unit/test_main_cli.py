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


def test_reload_warns_and_delegates_to_update(monkeypatch, capsys):
    from chronicle import __main__ as main_mod

    monkeypatch.setattr(main_mod, "_run_remote_install_script", lambda url: 0)
    monkeypatch.setattr(main_mod, "main", main_mod.main)
    monkeypatch.setattr(sys, "argv", ["chronicle", "reload"])

    with pytest.raises(SystemExit) as excinfo:
        main_mod.main()
    assert excinfo.value.code == 0
    assert "deprecated" in capsys.readouterr().err


def test_install_daemon_surfaces_service_error(monkeypatch, capsys):
    from chronicle import __main__ as main_mod
    import chronicle.service as service_mod

    fake_service = types.ModuleType("chronicle.service")
    fake_service._MAC_PLIST_PATH = "/tmp/fake.plist"
    fake_service._LINUX_UNIT_PATH = "/tmp/fake.service"
    fake_service.install_service = lambda: False
    fake_service.last_service_error = lambda: "launchctl bootstrap failed: permission denied"

    monkeypatch.setitem(sys.modules, "chronicle.service", fake_service)
    monkeypatch.setattr(service_mod, "install_service", fake_service.install_service)
    monkeypatch.setattr(service_mod, "last_service_error", fake_service.last_service_error)
    monkeypatch.setattr(service_mod, "_MAC_PLIST_PATH", fake_service._MAC_PLIST_PATH)
    monkeypatch.setattr(service_mod, "_LINUX_UNIT_PATH", fake_service._LINUX_UNIT_PATH)
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setattr("chronicle.mode.set_processing_mode", lambda mode: None)

    main_mod.install_daemon()
    err = capsys.readouterr().err
    assert "permission denied" in err
