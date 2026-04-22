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
        If the runtime binary exists but your shell cannot find `chronicle`,
        verify `~/.local/bin/chronicle` still points at `~/.chronicle/runtime/chronicle`.

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
        place, and restart the daemon if it's running. If `chronicle`
        itself no longer resolves in your shell, rerun `install.sh`
        instead so the `~/.local/bin/chronicle` symlink is recreated.
    chronicle uninstall [--purge] [--yes] [--dry-run]
        Remove chronicle from this machine. Stops/removes the daemon,
        strips chronicle-hook entries from ~/.claude/settings.json, and
        removes ~/.local/bin/chronicle{,-hook} + ~/.chronicle/runtime/.
        Preserves user data at ~/.chronicle/ (events.jsonl, config.json,
        .processed/, .failed/). Pass --purge to delete that too (prompts
        unless --yes). --dry-run shows the plan without executing.
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
    elif command == "uninstall":
        uninstall_install()
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


def uninstall_install():
    """Remove chronicle from this machine.

    Default: stop daemon, remove service file, strip chronicle-hook entries
    from ~/.claude/settings.json, remove ~/.local/bin/chronicle{,-hook}
    symlinks and ~/.chronicle/runtime/. Preserves user data.

    --purge: additionally rm -rf ~/.chronicle (events.jsonl, config, logs,
    processed/, failed/). Prompts for confirmation unless --yes.

    IMPORTANT: we import every chronicle.* dependency up-front. After we
    delete ~/.chronicle/runtime/, the PyInstaller --onedir bootstrap can no
    longer late-load modules — subsequent imports would crash the process
    mid-uninstall. Resolve everything while the runtime is still live.
    """
    import argparse
    import os
    import shutil as _shutil

    from pathlib import Path

    from . import service as _service
    from .config import chronicle_dir
    from .install_hooks import uninstall_hooks
    from .mode import set_processing_mode

    parser = argparse.ArgumentParser(
        prog="chronicle uninstall",
        description="Remove chronicle from this machine.",
    )
    parser.add_argument("--purge", action="store_true",
                        help="Also delete ~/.chronicle/ (events.jsonl, config, logs).")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the --purge confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be removed, don't remove anything.")
    args = parser.parse_args()

    home_dir = chronicle_dir()
    runtime_dir = home_dir / "runtime"
    legacy_src_dir = home_dir / "src"
    bin_dir = Path.home() / ".local" / "bin"
    settings_file = Path.home() / ".claude" / "settings.json"

    def _symlink_is_chronicle_owned(link: Path) -> bool:
        if not link.is_symlink():
            return False
        try:
            target = os.readlink(str(link))
        except OSError:
            return False
        if os.path.isabs(target):
            probe = Path(target)
        else:
            probe = (link.parent / target)
        try:
            probe_resolved = probe.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        runtime_resolved = runtime_dir.resolve(strict=False) if runtime_dir.exists() \
                           else runtime_dir
        try:
            probe_resolved.relative_to(runtime_resolved)
            return True
        except ValueError:
            return False

    # Build two separate plans so the output can say "Uninstalled" vs "Purged"
    # correctly instead of muddling everything into one list.
    plan_integration: list[str] = []   # service, hooks, symlinks, runtime, legacy src
    plan_data: list[str] = []          # home_dir purge (only with --purge)
    plan_preserved: list[str] = []     # shown only when integration exists and NOT purging
    plan_warn: list[str] = []

    if _service.service_installed():
        sfp = _service.service_file_path()
        plan_integration.append(f"daemon service file: {sfp}")

    hook_entries_to_strip = 0
    if settings_file.exists():
        try:
            hook_entries_to_strip = uninstall_hooks(str(settings_file), dry_run=True)
        except Exception as e:
            plan_warn.append(f"could not preview {settings_file}: {e}")
        if hook_entries_to_strip:
            plan_integration.append(
                f"{hook_entries_to_strip} chronicle-hook entries from {settings_file}"
            )

    symlinks_to_remove: list[Path] = []
    for name in ("chronicle", "chronicle-hook"):
        link = bin_dir / name
        if not (link.exists() or link.is_symlink()):
            continue
        if _symlink_is_chronicle_owned(link):
            symlinks_to_remove.append(link)
            plan_integration.append(str(link))
        else:
            try:
                tgt = os.readlink(str(link)) if link.is_symlink() else "(regular file)"
            except OSError:
                tgt = "?"
            plan_warn.append(
                f"{link} is not a chronicle-owned symlink (target: {tgt}); leaving it alone"
            )

    if runtime_dir.exists():
        plan_integration.append(f"{runtime_dir}/")

    if legacy_src_dir.exists():
        plan_integration.append(f"{legacy_src_dir}/ (legacy pre-v0.8.0 source install)")

    if args.purge and home_dir.exists():
        plan_data.append(f"{home_dir}/ (events.jsonl, config, logs, markers)")
    elif plan_integration and home_dir.exists():
        # Only meaningful to show "preserved" if we're actually uninstalling
        # something. If there's nothing to uninstall, preservation is noise.
        for name in ("events.jsonl", "config.json", ".processed", ".failed",
                     "projects", "daemon.log", "hook-errors.log", "install-errors.log"):
            p = home_dir / name
            if p.exists():
                plan_preserved.append(str(p))

    nothing_to_do = not plan_integration and not plan_data

    # ---------- Dry-run render ----------
    if args.dry_run:
        if nothing_to_do:
            print("chronicle is not installed on this machine. Nothing to do.")
            for item in plan_warn:
                print(f"  ! {item}")
            return
        print("DRY RUN — chronicle uninstall would do the following:\n")
        if plan_integration:
            print("Remove integration:")
            for item in plan_integration:
                print(f"  - {item}")
        if plan_data:
            if plan_integration:
                print()
            print("Purge data (--purge):")
            for item in plan_data:
                print(f"  - {item}")
        if plan_preserved:
            print("\nPreserve (use --purge to delete):")
            for item in plan_preserved:
                print(f"  - {item}")
        if plan_warn:
            print("\nWarnings:")
            for item in plan_warn:
                print(f"  ! {item}")
        return

    # ---------- Execution mode ----------
    if nothing_to_do:
        print("chronicle is not installed on this machine. Nothing to do.")
        for item in plan_warn:
            print(f"WARN: {item}", file=sys.stderr)
        return

    # Confirm --purge (only if there's actual data to delete)
    if plan_data and not args.yes:
        print(f"WARNING: --purge will delete ALL chronicle data under {home_dir}.")
        print("This includes events.jsonl, config.json, processed/failed markers,")
        print("per-project chronicles, and logs. This cannot be undone.\n")
        try:
            answer = input("Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            print("Aborted.")
            sys.exit(1)

    # Track execution separately so the summary can report them under
    # distinct headers ("Uninstalled:" vs "Purged data:").
    integration_done: list[str] = []
    data_done: list[str] = []

    # Flip processing_mode to foreground before removing the service so a
    # future reinstall on this box (same preserved config) doesn't boot
    # straight into stale background intent. Skip if we're purging anyway.
    if not args.purge and (home_dir / "config.json").exists():
        try:
            set_processing_mode("foreground")
        except Exception as e:
            print(f"WARN: could not reset processing_mode: {e}", file=sys.stderr)

    if _service.service_installed():
        try:
            _service.uninstall_service()
            integration_done.append("daemon service removed")
        except Exception as e:
            print(f"WARN: service uninstall failed: {e}", file=sys.stderr)

    if hook_entries_to_strip and settings_file.exists():
        try:
            removed = uninstall_hooks(str(settings_file), dry_run=False)
            integration_done.append(f"{removed} chronicle-hook entries removed from {settings_file}")
        except Exception as e:
            print(f"WARN: could not edit {settings_file}: {e}", file=sys.stderr)

    for link in symlinks_to_remove:
        try:
            link.unlink()
            integration_done.append(f"{link} removed")
        except OSError as e:
            print(f"WARN: could not remove {link}: {e}", file=sys.stderr)

    if legacy_src_dir.exists():
        _shutil.rmtree(legacy_src_dir, ignore_errors=True)
        integration_done.append(f"{legacy_src_dir}/ removed")

    # Runtime removal is the LAST non-purge step. Every chronicle.* module
    # we might still need has already been imported above; do not call into
    # chronicle.anything after this point.
    if runtime_dir.exists():
        _shutil.rmtree(runtime_dir, ignore_errors=True)
        integration_done.append(f"{runtime_dir}/ removed")

    if args.purge and home_dir.exists():
        _shutil.rmtree(home_dir, ignore_errors=True)
        data_done.append(f"{home_dir}/ purged")

    # ---------- Summary ----------
    if integration_done:
        print("Uninstalled:")
        for item in integration_done:
            print(f"  - {item}")
    if data_done:
        if integration_done:
            print()
        print("Purged data:")
        for item in data_done:
            print(f"  - {item}")

    # "Preserved" footer only makes sense if we actually uninstalled
    # something. If every integration step failed, saying "Preserved" would
    # misleadingly suggest clean success.
    if integration_done and not args.purge and home_dir.exists():
        print(f"\nPreserved user data at {home_dir}")
        print(f"To delete it later:  rm -rf {home_dir}")

    # Final message branches on what actually happened, not on what was planned.
    if integration_done and data_done:
        print("\nchronicle has been removed and all data purged.")
    elif integration_done:
        print("\nchronicle has been removed. Restart Claude Code so the stripped hooks take effect.")
    elif data_done:
        print("\nLeftover chronicle data purged.")
    else:
        # Had a plan but nothing succeeded (all steps raised / rmtree failed).
        # Don't print a cheerful success — exit nonzero so CI / scripts notice.
        print("\nERROR: planned uninstall steps did not complete. See WARN messages above.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
