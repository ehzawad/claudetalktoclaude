"""Single-binary dispatch entry point for PyInstaller builds.

The frozen binary is shipped as `chronicle` and symlinked to `chronicle-hook`
at install time (busybox pattern). sys.argv[0] basename picks which command
to run.

Unix-only by design: macOS (Apple Silicon) and Linux (x86_64) are the
supported targets, so there is no Windows suffix handling here.
"""

import os
import sys


def main():
    os.umask(0o077)  # owner-only perms for everything chronicle writes (BUG-25)
    prog = os.path.basename(sys.argv[0]).lower()
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
