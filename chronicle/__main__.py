"""Decision Chronicle — persistent session knowledge tracker for Claude Code.

Two processing modes:
  foreground  (default) — no daemon, summarize on demand via `chronicle process`.
  background            — daemon auto-summarizes. Enable with `chronicle install-daemon`.

Usage:
    chronicle process [--project NAME] [--workers N] [--force] [--retry-failed] [--dry-run]
        Summarize pending sessions. --retry-failed retries terminal failures
        after the underlying issue has been fixed. --force reprocesses
        already-successful sessions.

    chronicle query projects
        Per-project counts: processed / pending / terminal-failed.
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

    chronicle doctor [--json]
        Diagnose: mode, resolved claude binary, daemon/service status,
        drift warnings, counts. --json emits a schema-versioned document
        (top-level `ok: bool`, `schema_version: 1`) for CI health checks.

    chronicle install-daemon
        Switch to background mode: install & start launchd/systemd service.
    chronicle uninstall-daemon
        Switch to foreground mode: stop & remove launchd/systemd service.

    chronicle daemon [--bg|--stop|--status]
        Internal / manual daemon control. Normal mode switching is
        `install-daemon` / `uninstall-daemon` above — which manages the
        service manager for you.

    chronicle update
        Download the latest release binary, verify SHA256, swap it into
        place, and restart the daemon if it's running.
    chronicle install-hooks [settings-path]
        Install chronicle hooks into Claude Code's settings.json. Defaults to
        ~/.claude/settings.json. Called by install.sh; safe to re-run.

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
        # Deprecated: the installed artifact is a self-contained binary, there
        # is no local source tree to reinstall from. Redirect to `update`.
        print("`chronicle reload` is deprecated in binary builds; use `chronicle update`.",
              file=sys.stderr)
        update_install()
    elif command == "update":
        update_install()
    elif command == "install-hooks":
        from pathlib import Path
        from .install_hooks import install_hooks
        default_path = str(Path.home() / ".claude" / "settings.json")
        install_hooks(sys.argv[1] if len(sys.argv) >= 2 else default_path)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


def install_daemon():
    """Switch to background mode: flip config BEFORE starting the service,
    so the freshly-spawned daemon sees background on its very first tick
    rather than booting, idling in foreground, then flipping later.
    """
    from . import service
    from .mode import set_processing_mode

    # Flip mode first — the daemon re-reads config on every loop iteration
    # and the self-disable check happens at the top of that loop, so
    # ordering matters.
    set_processing_mode("background")
    try:
        accepted = service.install_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        # Roll back mode so doctor doesn't lie about intent.
        set_processing_mode("foreground")
        sys.exit(1)

    if accepted:
        print("Installed background daemon and set processing_mode=background.")
    else:
        print("Service file written, but the service manager did NOT start the daemon cleanly.",
              file=sys.stderr)
        print("processing_mode=background is set; run `chronicle doctor` for details.",
              file=sys.stderr)
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


def update_install():
    """Re-run install.sh to fetch and install the latest release binary.

    install.sh already handles: platform detection, asset download, checksum
    verification, symlink placement, macOS quarantine cleanup, hook config,
    and daemon restart (via launchctl kickstart / systemctl restart). Rather
    than duplicate all of that here, we pipe it back through bash — one
    source of truth for install and update.
    """
    import subprocess
    url = "https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh"
    rc = subprocess.call(f"curl -fsSL {url} | bash", shell=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
