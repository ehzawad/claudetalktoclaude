"""Unit tests for chronicle.service.mode_drift_warnings.

Pure decision logic: given (mode, service_installed, service_running),
return the right set of human-readable warning strings. We monkeypatch
the three state-sensors so these tests don't touch launchctl/systemctl
or the filesystem.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def mocked_service(monkeypatch, tmp_path):
    """Isolate HOME + provide knobs for (mode, installed, running)."""
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    import importlib
    import chronicle.config
    import chronicle.mode
    import chronicle.service
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.mode)
    importlib.reload(chronicle.service)

    from chronicle import mode as mode_mod
    from chronicle import service as svc_mod

    def set_state(*, processing_mode: str,
                  installed: bool, running: bool) -> list[str]:
        mode_mod.set_processing_mode(processing_mode)
        monkeypatch.setattr(svc_mod, "service_installed", lambda: installed)
        monkeypatch.setattr(svc_mod, "service_running", lambda: running)
        return svc_mod.mode_drift_warnings()

    yield set_state


class TestModeDriftWarnings:
    def test_foreground_clean(self, mocked_service):
        warnings = mocked_service(
            processing_mode="foreground", installed=False, running=False,
        )
        assert warnings == []

    def test_foreground_with_service_file(self, mocked_service):
        warnings = mocked_service(
            processing_mode="foreground", installed=True, running=False,
        )
        assert len(warnings) == 1
        assert "foreground" in warnings[0].lower()
        assert "service file present" in warnings[0]
        assert "chronicle uninstall-daemon" in warnings[0]

    def test_foreground_with_running_daemon(self, mocked_service):
        warnings = mocked_service(
            processing_mode="foreground", installed=False, running=True,
        )
        assert len(warnings) == 1
        assert "foreground" in warnings[0].lower()
        assert "daemon running" in warnings[0]

    def test_foreground_with_installed_and_running(self, mocked_service):
        warnings = mocked_service(
            processing_mode="foreground", installed=True, running=True,
        )
        assert len(warnings) == 1
        # Both conditions mentioned in the same warning
        assert "service file present" in warnings[0]
        assert "daemon running" in warnings[0]

    def test_background_missing_service_file(self, mocked_service):
        warnings = mocked_service(
            processing_mode="background", installed=False, running=False,
        )
        assert len(warnings) == 1
        assert "background" in warnings[0].lower()
        assert "service file missing" in warnings[0]
        assert "chronicle install-daemon" in warnings[0]

    def test_background_installed_but_not_running(self, mocked_service):
        warnings = mocked_service(
            processing_mode="background", installed=True, running=False,
        )
        assert len(warnings) == 1
        assert "background" in warnings[0].lower()
        assert "daemon not running" in warnings[0]

    def test_background_fully_healthy(self, mocked_service):
        warnings = mocked_service(
            processing_mode="background", installed=True, running=True,
        )
        assert warnings == []


def test_chronicle_binary_raises_when_missing(tmp_home, monkeypatch):
    import importlib
    import chronicle.service as service

    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    importlib.reload(service)

    with pytest.raises(RuntimeError) as excinfo:
        service._chronicle_binary()
    assert "chronicle binary not found" in str(excinfo.value)


def test_install_service_records_bootstrap_error(monkeypatch):
    import chronicle.service as service

    monkeypatch.setattr(service, "platform_key", lambda: "macos")

    def fake_mac_install():
        service._LAST_SERVICE_ERROR = "launchctl bootstrap failed: boom"
        return False

    monkeypatch.setattr(service, "_mac_install", fake_mac_install)

    accepted = service.install_service()
    assert accepted is False
    assert service.last_service_error() == "launchctl bootstrap failed: boom"
