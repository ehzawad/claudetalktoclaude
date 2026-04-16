"""Decision Chronicle — persistent session knowledge tracker for Claude Code.

Two processing modes:
  foreground  (default) — no daemon, summarize on demand via `chronicle process`.
  background            — daemon auto-summarizes. Enable with `chronicle install-daemon`.

Usage:
    chronicle process [--project NAME] [--workers N] [--force] [--retry-failed] [--dry-run]
        Process sessions into chronicle records. Runs claude -p to summarize.

    chronicle query projects
        List all chronicled projects and any pending ones.
    chronicle query sessions [PATH]
        Show chronicle.md and session files for a project.
    chronicle query timeline [--limit N]
        Recent sessions across all projects, newest first.
    chronicle query search "term"
        Full-text search across all chronicle markdown files.

    chronicle rewind [N] [--since N] [--diff N] [--summary N]
        Navigate session history. View, compare, or summarize sessions.
    chronicle rewind --delete N
        Delete a session record. --prune deletes all 0-decision sessions.

    chronicle insight [project-name]
        Generate an LLM-powered HTML dashboard and open in browser.
    chronicle story [project-name]
        Generate a unified project narrative (story.md) for stakeholders.

    chronicle doctor
        Diagnose: mode, resolved claude binary, daemon/service status, counts.

    chronicle daemon [--bg|--stop|--status]
        Manage the background daemon process (in background mode).
    chronicle install-daemon
        Switch to background mode: install & start launchd/systemd service.
    chronicle uninstall-daemon
        Switch to foreground mode: stop & remove launchd/systemd service.
    chronicle reload
        Reinstall from source, fix symlinks, restart daemon if running.

    chronicle --version
"""

import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]

    if command in ("--version", "-V"):
        from . import __version__
        print(f"chronicle {__version__}")
        sys.exit(0)
    # Shift argv so submodule parsers see correct args
    sys.argv = [f"chronicle.{command}"] + sys.argv[2:]

    if command == "daemon":
        from .daemon import main as daemon_main
        daemon_main()
    elif command in ("process", "batch"):
        from .batch import main as batch_main
        batch_main()
    elif command == "query":
        from .query import main as query_main
        query_main()
    elif command == "rewind":
        from .rewind import main as rewind_main
        rewind_main()
    elif command == "insight":
        from .insight import main as insight_main
        insight_main()
    elif command == "story":
        from .story import main as story_main
        story_main()
    elif command == "install-daemon":
        install_daemon()
    elif command == "uninstall-daemon":
        uninstall_daemon()
    elif command == "doctor":
        from .doctor import run as doctor_run
        sys.exit(doctor_run())
    elif command == "reload":
        reload_install()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


def install_daemon():
    """Switch to background mode: write service file + bootstrap + set config."""
    from . import service
    from .mode import set_processing_mode

    try:
        service.install_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    set_processing_mode("background")

    print("Installed background daemon and set processing_mode=background.")
    print()
    if sys.platform == "darwin":
        print("macOS launchd service:")
        print(f"  {service._MAC_PLIST_PATH}")
        print("Manage:")
        print("  launchctl print gui/$UID/com.chronicle.daemon")
        print("  launchctl bootout gui/$UID/com.chronicle.daemon")
    elif sys.platform.startswith("linux"):
        print("Linux systemd --user service:")
        print(f"  {service._LINUX_UNIT_PATH}")
        print("Manage:")
        print("  systemctl --user status chronicle-daemon.service")
        print("  journalctl --user -u chronicle-daemon.service -f")
        print()
        print("Note: on Ubuntu 24.04, enable user-service persistence with")
        print("  sudo loginctl enable-linger $USER")
    print()
    print("Verify:  chronicle doctor")


def uninstall_daemon():
    """Switch to foreground mode: remove service file + update config."""
    from . import service
    from .mode import set_processing_mode

    try:
        service.uninstall_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    set_processing_mode("foreground")
    print("Uninstalled background daemon and set processing_mode=foreground.")
    print()
    print("Verify:  chronicle doctor")
    print("Note: hooks still record session events and inject past titles;")
    print("      run `chronicle process` to summarize on demand.")


def reload_install():
    """Reinstall from the current source, fix symlinks, and restart the daemon."""
    import os
    import signal
    import subprocess
    from pathlib import Path

    from .daemon import _is_running

    src_dir = Path(__file__).resolve().parent.parent
    venv_dir = src_dir / ".venv"
    bin_dir = Path.home() / ".local" / "bin"

    # Stop running daemon so it picks up new code on restart
    daemon_was_running = False
    running, pid = _is_running()
    if running:
        os.kill(pid, signal.SIGTERM)
        daemon_was_running = True
        print(f"Stopped daemon (pid {pid}) for reload.")

    print(f"Reinstalling from {src_dir}...")

    # Ensure venv exists
    if not venv_dir.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    # Reinstall
    pip = venv_dir / "bin" / "pip"
    result = subprocess.run(
        [str(pip), "install", "-e", str(src_dir), "--quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"pip install failed: {result.stderr}")
        sys.exit(1)

    # Fix symlinks
    bin_dir.mkdir(parents=True, exist_ok=True)
    for cmd in ("chronicle", "chronicle-hook"):
        link = bin_dir / cmd
        target = venv_dir / "bin" / cmd
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    print(f"Symlinks updated:")
    print(f"  {bin_dir / 'chronicle'} -> {venv_dir / 'bin' / 'chronicle'}")
    print(f"  {bin_dir / 'chronicle-hook'} -> {venv_dir / 'bin' / 'chronicle-hook'}")

    # Configure hooks
    from .install_hooks import install_hooks
    settings_file = Path.home() / ".claude" / "settings.json"
    install_hooks(str(settings_file))

    # Restart daemon if it was running before reload
    if daemon_was_running:
        from .config import CHRONICLE_DIR
        log_file = CHRONICLE_DIR / "daemon.log"
        with open(log_file, "a") as log_fd:
            subprocess.Popen(
                [str(venv_dir / "bin" / "python"), "-m", "chronicle.daemon"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
            )
        print("Restarted daemon with new code.")

    print("\nReload complete.")


if __name__ == "__main__":
    main()
