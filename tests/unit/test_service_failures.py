"""Service-manager failure contract: distinct causes get distinct diagnoses."""
from __future__ import annotations

import subprocess

import pytest

import chronicle.service as service


_REAL_CHRONICLE_BINARY = service._chronicle_binary


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_MAC_PLIST_PATH", tmp_path / "com.chronicle.daemon.plist")
    monkeypatch.setattr(service, "_LINUX_UNIT_PATH", tmp_path / "chronicle-daemon.service")
    monkeypatch.setattr(service, "_chronicle_binary", lambda: "/bin/echo")
    yield


def test_chronicle_binary_rejects_non_executable(tmp_path, monkeypatch):
    """A 0644 file on PATH would be baked into the unit file and fail at boot."""
    binary = tmp_path / "chronicle"
    binary.write_text("not executable")
    binary.chmod(0o644)
    monkeypatch.setattr(service.shutil, "which", lambda name: str(binary))

    with pytest.raises(RuntimeError, match="not an executable file"):
        _REAL_CHRONICLE_BINARY()


def test_chronicle_binary_accepts_executable(tmp_path, monkeypatch):
    binary = tmp_path / "chronicle"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(service.shutil, "which", lambda name: str(binary))

    assert _REAL_CHRONICLE_BINARY() == str(binary.resolve())


def test_manager_rejection_raises_so_cli_exits_nonzero(monkeypatch):
    monkeypatch.setattr(service, "platform_key", lambda: "macos")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/bin/launchctl")
    monkeypatch.setattr(service, "_mac_bootout", lambda: None)
    monkeypatch.setattr(service, "_mac_bootstrap", lambda: subprocess.CompletedProcess(
        ["launchctl"], 1, stdout="", stderr="Load failed: 5: Input/output error"))

    with pytest.raises(RuntimeError) as excinfo:
        service.install_service()
    assert "Load failed" in str(excinfo.value)
    assert service.last_service_error().startswith("launchctl bootstrap failed")


@pytest.mark.parametrize("platform,manager", [("macos", "launchctl"), ("linux", "systemctl")])
def test_missing_manager_is_a_distinct_exception(monkeypatch, platform, manager):
    monkeypatch.setattr(service, "platform_key", lambda: platform)
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    with pytest.raises(service.ServiceManagerUnavailable) as excinfo:
        service.install_service()
    assert manager in str(excinfo.value)
    # WSL guidance belongs only on the systemd path.
    assert ("WSL2" in str(excinfo.value)) is (manager == "systemctl")
    # Nothing was written before the guard tripped.
    assert not service._MAC_PLIST_PATH.exists()
    assert not service._LINUX_UNIT_PATH.exists()


@pytest.mark.parametrize("platform,manager", [("macos", "launchctl"), ("linux", "systemctl")])
def test_filesystem_failure_is_not_blamed_on_the_service_manager(monkeypatch, platform, manager):
    """A read-only HOME / ENOSPC used to surface as 'systemctl unavailable ...
    on WSL2, enable systemd', sending users toward entirely the wrong repair."""
    monkeypatch.setattr(service, "platform_key", lambda: platform)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/bin/{manager}")

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service.Path, "write_text", boom)

    with pytest.raises(RuntimeError) as excinfo:
        service.install_service()
    message = str(excinfo.value)
    assert "could not write service file" in message
    assert "No space left on device" in message
    assert "WSL2" not in message
    assert not isinstance(excinfo.value, service.ServiceManagerUnavailable)


def test_linux_uninstall_removes_unit_without_systemctl(monkeypatch):
    service._LINUX_UNIT_PATH.write_text("[Service]\n")
    monkeypatch.setattr(service, "platform_key", lambda: "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    service.uninstall_service()
    assert not service._LINUX_UNIT_PATH.exists()


def test_macos_uninstall_removes_plist_without_launchctl(monkeypatch):
    """Symmetry with Linux: a missing launchctl must not strand the plist,
    or `chronicle doctor` reports drift forever with no way to clear it."""
    service._MAC_PLIST_PATH.write_text("<plist/>")
    monkeypatch.setattr(service, "platform_key", lambda: "macos")
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    service.uninstall_service()
    assert not service._MAC_PLIST_PATH.exists()


def test_macos_uninstall_removes_plist_even_if_bootout_raises(monkeypatch):
    service._MAC_PLIST_PATH.write_text("<plist/>")
    monkeypatch.setattr(service, "platform_key", lambda: "macos")
    monkeypatch.setattr(service.shutil, "which", lambda name: "/bin/launchctl")

    def boom():
        raise FileNotFoundError(2, "No such file or directory", "launchctl")

    monkeypatch.setattr(service, "_mac_bootout", boom)

    with pytest.raises(FileNotFoundError):
        service.uninstall_service()
    assert not service._MAC_PLIST_PATH.exists()
