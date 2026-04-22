"""Platform service-manager integration (launchd + systemd-user).

Responsibilities:
- Write/remove launchd plist (macOS) or systemd user unit (Linux).
- Bootstrap/bootout (macOS) or enable/disable (Linux) the service.
- Pause/resume service during `chronicle process` to prevent races.
- Detect mode drift (config says foreground but service loaded, etc.).

Designed to work under both macOS Tahoe (launchd) and Ubuntu 24.04 LTS
(systemd --user). Status probes (`service_running`, `service_installed`,
`mode_drift_warnings`) are best-effort — missing `launchctl`/`systemctl`
degrades to "unknown" rather than raising. Install / bootstrap surfaces
its failure: `install_service()` returns False when the manager rejected
the job, and `install-daemon` rolls the config mode back so `chronicle
doctor` doesn't lie about intent. The processing lock is the correctness
boundary across all code paths.

Service files always include a full PATH in EnvironmentVariables /
Environment="PATH=..." so the daemon can find `claude` even when
launched by a service manager that doesn't source shell profiles.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .claude_cli import try_resolve_claude_binary

_MAC_LABEL = "com.chronicle.daemon"
_LINUX_UNIT = "chronicle-daemon.service"
_LAST_SERVICE_ERROR: Optional[str] = None

_MAC_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
_LINUX_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / _LINUX_UNIT


def _standard_path() -> str:
    """PATH string to bake into service unit files."""
    parts = [
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    # Preserve anything unique from the current PATH too (helps nvm/pyenv)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def _set_last_service_error(message: Optional[str]) -> None:
    global _LAST_SERVICE_ERROR
    _LAST_SERVICE_ERROR = message


def last_service_error() -> Optional[str]:
    return _LAST_SERVICE_ERROR


def _describe_process_failure(res: subprocess.CompletedProcess, prefix: str) -> str:
    detail = (res.stderr or res.stdout or "").strip()
    if detail:
        return f"{prefix}: {detail}"
    return f"{prefix} (exit {res.returncode})"


def _chronicle_binary() -> str:
    """Absolute path to the chronicle entry point used in launchd / systemd unit files.

    For frozen PyInstaller builds, prefer sys.executable — that's the actual
    binary running right now. shutil.which can pick up a dev checkout, a
    stale symlink, or an older release on PATH and bake that into the
    service file, leading to subtle drift.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable)
    else:
        found = shutil.which("chronicle")
        candidate = Path(found) if found else Path.home() / ".local" / "bin" / "chronicle"

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"chronicle binary not found at {candidate}; rerun install.sh or `chronicle update` first."
        ) from e
    if not resolved.is_file():
        raise RuntimeError(
            f"chronicle binary path {resolved} is not an executable file."
        )
    return str(resolved)


# ---------- macOS (launchd) ----------

def _mac_plist_contents() -> str:
    chronicle_bin = _chronicle_binary()
    home = str(Path.home())
    path_val = _standard_path()
    claude = try_resolve_claude_binary()
    claude_hint = f"    <!-- resolved claude at install: {claude} -->\n" if claude else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
{claude_hint}    <key>Label</key>
    <string>{_MAC_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{chronicle_bin}</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{home}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_val}</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{home}/.chronicle/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/.chronicle/daemon.log</string>
</dict>
</plist>
"""


def _mac_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _mac_bootout() -> None:
    """Bootout the service if loaded; ignore errors if not loaded."""
    uid = os.getuid()
    _mac_run(["launchctl", "bootout", f"gui/{uid}/{_MAC_LABEL}"])


def _mac_bootstrap() -> subprocess.CompletedProcess:
    """Bootstrap the service."""
    uid = os.getuid()
    return _mac_run(["launchctl", "bootstrap", f"gui/{uid}", str(_MAC_PLIST_PATH)])


def _mac_is_loaded() -> bool:
    res = _mac_run(["launchctl", "print", f"gui/{os.getuid()}/{_MAC_LABEL}"])
    return res.returncode == 0


def _mac_install() -> bool:
    """Write plist and (re)bootstrap. Returns True if launchd accepted the job."""
    _MAC_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MAC_PLIST_PATH.write_text(_mac_plist_contents())
    _mac_bootout()
    res = _mac_bootstrap()
    if res.returncode != 0:
        _set_last_service_error(_describe_process_failure(res, "launchctl bootstrap failed"))
        return False
    return True


def _mac_uninstall() -> None:
    _mac_bootout()
    if _MAC_PLIST_PATH.exists():
        _MAC_PLIST_PATH.unlink()


# ---------- Linux (systemd --user) ----------

def _linux_unit_contents() -> str:
    chronicle_bin = _chronicle_binary()
    path_val = _standard_path()
    return f"""[Unit]
Description=Decision Chronicle Daemon
After=default.target

[Service]
Type=simple
WorkingDirectory=%h
Environment="PATH={path_val}"
ExecStart={chronicle_bin} daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _linux_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _linux_is_active() -> bool:
    res = _linux_run(["systemctl", "--user", "is-active", _LINUX_UNIT])
    return res.returncode == 0


def _linux_install() -> bool:
    """Write unit and `enable --now`. Returns True if systemctl reports success."""
    _LINUX_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LINUX_UNIT_PATH.write_text(_linux_unit_contents())
    reload_res = _linux_run(["systemctl", "--user", "daemon-reload"])
    if reload_res.returncode != 0:
        _set_last_service_error(_describe_process_failure(reload_res, "systemctl daemon-reload failed"))
        return False
    res = _linux_run(["systemctl", "--user", "enable", "--now", _LINUX_UNIT])
    if res.returncode != 0:
        _set_last_service_error(_describe_process_failure(res, "systemctl enable --now failed"))
        return False
    return True


def _linux_uninstall() -> None:
    _linux_run(["systemctl", "--user", "disable", "--now", _LINUX_UNIT])
    if _LINUX_UNIT_PATH.exists():
        _LINUX_UNIT_PATH.unlink()
    _linux_run(["systemctl", "--user", "daemon-reload"])


# ---------- Public API ----------

def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def install_service() -> bool:
    """Install and start the service on this platform. Idempotent.

    Returns True if the service manager accepted the job. False means
    the file was written but bootstrap/enable failed — caller should
    surface this to the user via `chronicle doctor`.
    """
    _set_last_service_error(None)
    p = platform_key()
    if p == "macos":
        return _mac_install()
    if p == "linux":
        return _linux_install()
    raise RuntimeError(
        f"Unsupported platform {sys.platform}; run `chronicle daemon` manually."
    )


def uninstall_service() -> None:
    """Stop and remove the service on this platform. Idempotent."""
    p = platform_key()
    if p == "macos":
        _mac_uninstall()
    elif p == "linux":
        _linux_uninstall()
    else:
        # Nothing to uninstall on unknown platform
        return


def service_installed() -> bool:
    """Is the service file present on disk?"""
    p = platform_key()
    if p == "macos":
        return _MAC_PLIST_PATH.exists()
    if p == "linux":
        return _LINUX_UNIT_PATH.exists()
    return False


def service_running() -> bool:
    """Is the service currently loaded/active per the service manager?"""
    p = platform_key()
    if p == "macos":
        if not shutil.which("launchctl"):
            return False
        return _mac_is_loaded()
    if p == "linux":
        if not shutil.which("systemctl"):
            return False
        return _linux_is_active()
    return False


def service_file_path() -> Optional[Path]:
    """Path to the service unit file for this platform."""
    p = platform_key()
    if p == "macos":
        return _MAC_PLIST_PATH
    if p == "linux":
        return _LINUX_UNIT_PATH
    return None


def pause_service() -> bool:
    """Stop the service without removing the file (for `chronicle process`).

    Returns True if the service was paused (and therefore should be resumed),
    False otherwise.
    """
    p = platform_key()
    if p == "macos":
        if not shutil.which("launchctl"):
            return False
        was_running = _mac_is_loaded()
        _mac_bootout()
        return was_running
    if p == "linux":
        if not shutil.which("systemctl"):
            return False
        was_active = _linux_is_active()
        if was_active:
            _linux_run(["systemctl", "--user", "stop", _LINUX_UNIT])
        return was_active
    return False


def resume_service() -> None:
    """Re-bootstrap / re-start the service. Called after a pause."""
    p = platform_key()
    if p == "macos":
        if _MAC_PLIST_PATH.exists() and shutil.which("launchctl"):
            _mac_bootstrap()
    elif p == "linux":
        if _LINUX_UNIT_PATH.exists() and shutil.which("systemctl"):
            _linux_run(["systemctl", "--user", "start", _LINUX_UNIT])


def mode_drift_warnings() -> list[str]:
    """Return human-readable warnings about config/service mismatch.

    Call from `chronicle doctor`.
    """
    from .mode import get_processing_mode  # local import avoids cycle

    warnings: list[str] = []
    mode = get_processing_mode()
    installed = service_installed()
    running = service_running()

    if mode == "foreground" and (installed or running):
        bits = []
        if installed:
            bits.append("service file present")
        if running:
            bits.append("daemon running")
        warnings.append(
            f"Mode=foreground but {', '.join(bits)} — "
            "run `chronicle uninstall-daemon` to fix."
        )
    elif mode == "background" and not installed:
        warnings.append(
            "Mode=background but service file missing — "
            "run `chronicle install-daemon` to fix."
        )
    elif mode == "background" and installed and not running:
        warnings.append(
            "Mode=background and service file present, but daemon not running. "
            "Check daemon.log; re-run `chronicle install-daemon` to reinstall the service."
        )
    return warnings
