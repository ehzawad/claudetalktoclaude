"""Single-binary dispatch entry point for PyInstaller builds.

The frozen binary is shipped as `chronicle` and symlinked to `chronicle-hook`
at install time (busybox pattern). sys.argv[0] basename picks which command
to run, so a single ~10MB binary replaces the two venv console scripts AND
the self-healing shell wrappers.
"""

import os
import sys


def main():
    prog = os.path.basename(sys.argv[0]).lower()
    # Strip macOS code-signing suffix if present (.exe is Windows, but harmless).
    for suffix in (".exe",):
        if prog.endswith(suffix):
            prog = prog[: -len(suffix)]

    if prog == "chronicle-hook":
        from chronicle.hook import main as hook_main
        raise SystemExit(hook_main())
    # Default: CLI. Covers "chronicle" and any unknown argv[0] (dev runs,
    # symlinks, etc.) — the CLI's own dispatcher will print usage for bad
    # invocations.
    from chronicle.__main__ import main as cli_main
    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
