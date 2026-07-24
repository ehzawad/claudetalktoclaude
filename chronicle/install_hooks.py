"""Configure chronicle hooks in Claude Code settings.json.

Called by install.sh. Merges hooks into existing settings without
overwriting other keys. Also exposes uninstall_hooks() for the
`chronicle uninstall` subcommand.

Hook entries use Claude Code's *exec form*: `args` is present, so `command`
is spawned directly as an executable with no `sh -c` layer. Per the hooks
reference, "A command hook runs as exec form when `args` is set, and shell
form when `args` is omitted." Exec form matters because command hooks run
outside an interactive shell, so a bare `chronicle-hook` would depend on
whatever PATH the Claude Code process happened to inherit — which fails when
Claude Code is launched from the macOS GUI or an IDE rather than a terminal.

`matcher` is omitted: the reference documents an omitted matcher as "match
all", which is what these lifecycle events want.
"""

import json
import os
import shutil
import sys
from pathlib import Path

# Set by install.sh to the symlink it is about to create. Needed because the
# installer configures hooks BEFORE placing the symlink, so no amount of
# probing the filesystem can discover the right path at that moment.
_HOOK_PATH_ENV = "CHRONICLE_HOOK_PATH"


def _chronicle_hook_path() -> Path:
    """Resolve the chronicle-hook executable to write into settings.json.

    Resolution order, most authoritative first:

    1. $CHRONICLE_HOOK_PATH — set by install.sh, which knows the path it is
       about to create. Allowed not to exist yet.
    2. Sibling of a frozen binary, if it exists.
    3. shutil.which() — finds console-script installs (pip / uv / venv /
       Homebrew), where pyproject.toml's `chronicle-hook` entry point lands
       somewhere other than ~/.local/bin.
    4. The install.sh default.

    Resolved lazily so tests and installs that override HOME see fresh paths.
    """
    env = os.environ.get(_HOOK_PATH_ENV)
    if env:
        return Path(env).expanduser()

    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).resolve().parent / "chronicle-hook"
        if sibling.exists():
            return sibling

    found = shutil.which("chronicle-hook")
    if found:
        return Path(found)

    return Path.home() / ".local" / "bin" / "chronicle-hook"


def _chronicle_hooks() -> dict:
    """Build the canonical Claude Code hook configuration for this user."""
    command = str(_chronicle_hook_path())
    sync_hook = {
        "type": "command",
        "command": command,
        "args": [],
        "statusMessage": "Loading chronicle context...",
    }
    async_hook = {
        "type": "command",
        "command": command,
        "args": [],
        "async": True,
    }
    return {
        "SessionStart": [{"hooks": [dict(sync_hook)]}],
        "Stop": [{"hooks": [dict(async_hook)]}],
        "UserPromptSubmit": [{"hooks": [dict(async_hook)]}],
        "SessionEnd": [{"hooks": [dict(async_hook)]}],
    }


def _invalid_hooks_error(path: Path, detail: str) -> None:
    print(
        f"ERROR: {path} has an invalid hooks structure ({detail}). "
        "Chronicle will not overwrite it. Fix the file and retry.",
        file=sys.stderr,
    )
    sys.exit(2)


def install_hooks(settings_path: str):
    path = Path(settings_path)

    if path.exists():
        try:
            raw = path.read_text()
            settings = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            # Don't silently clobber the user's settings — refuse and
            # tell them exactly where to look.
            print(
                f"ERROR: {path} is not valid JSON ({e}).\n"
                f"Chronicle will not overwrite it. Fix the file, or back it up and retry:\n"
                f"  cp {path} {path}.bak && echo '{{}}' > {path}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        settings = {}

    if not isinstance(settings, dict):
        print(
            f"ERROR: {path} top-level JSON is not an object. "
            f"Refusing to overwrite. Back it up and fix manually.",
            file=sys.stderr,
        )
        sys.exit(2)

    hooks = settings.get("hooks", {})
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        _invalid_hooks_error(path, "'hooks' must be an object")

    # Merge Chronicle hooks into existing hooks without replacing user entries.
    # For each event, remove only the Chronicle hook entries from existing
    # matcher groups, preserve unrelated user hooks, then append Chronicle's
    # canonical group.
    for event_name, chronicle_matchers in _chronicle_hooks().items():
        existing = hooks.get(event_name, [])
        if existing is None:
            existing = []
        if not isinstance(existing, list):
            _invalid_hooks_error(path, f"hooks[{event_name!r}] must be a list")

        cleaned_groups = []
        for mg in existing:
            if not isinstance(mg, dict):
                _invalid_hooks_error(path, f"hooks[{event_name!r}] must contain objects")
            entries = mg.get("hooks")
            if entries is None:
                _invalid_hooks_error(path, f"hooks[{event_name!r}] matcher groups need a 'hooks' list")
            if not isinstance(entries, list):
                _invalid_hooks_error(path, f"hooks[{event_name!r}][].hooks must be a list")

            kept_entries = []
            for entry in entries:
                if _is_chronicle_hook_entry(entry):
                    continue
                kept_entries.append(entry)

            if kept_entries:
                new_group = dict(mg)
                new_group["hooks"] = kept_entries
                cleaned_groups.append(new_group)

        hooks[event_name] = cleaned_groups + chronicle_matchers

    settings["hooks"] = hooks

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Configured hooks in {path}")

    # Exec form has no shell and therefore no PATH fallback: a wrong path is a
    # silently dead hook. install.sh legitimately configures hooks before it
    # creates the symlink, so only warn when nobody told us the path.
    hook_path = _chronicle_hook_path()
    if not os.environ.get(_HOOK_PATH_ENV) and not hook_path.exists():
        print(
            f"WARN: {hook_path} does not exist. Claude Code spawns this path "
            f"directly (exec form), so the hooks will not run until it does. "
            f"Re-run install.sh, or set {_HOOK_PATH_ENV} to the correct "
            f"chronicle-hook executable and re-run `chronicle install-hooks`.",
            file=sys.stderr,
        )


def _is_chronicle_hook_entry(entry) -> bool:
    """True if a settings.json hook entry is one Chronicle installed.

    Exactly one shape matches: Claude Code's exec form, i.e. an `args` key is
    present (its contents are irrelevant; `args: []` is still exec form). The
    whole `command` string is then one executable path, which may legitimately
    contain spaces ('/home/First Last/.local/bin/chronicle-hook'), so it is
    basename-compared as-is and never shell-tokenized.

    A missing `type` is tolerated (Claude Code defaults it to "command"); only
    an explicitly different type is rejected. That is defensive parsing of a
    hand-edited settings.json, not backwards compatibility — refusing to match
    a `type`-less entry would strand it in install, uninstall, AND doctor.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("type", "command") != "command":
        return False
    if "args" not in entry:
        return False
    cmd = entry.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    return os.path.basename(cmd.strip()) == "chronicle-hook"


def uninstall_hooks(settings_path: str, dry_run: bool = False) -> int:
    """Remove chronicle-hook entries from Claude Code settings.json.

    Subtractive: removes individual hook entries whose command invokes
    chronicle-hook. Preserves other hook entries users may have placed in
    the same matcher group. Drops matcher groups that become empty. Drops
    events that become empty. If the whole 'hooks' key becomes empty,
    drops it too.

    Returns the number of chronicle-hook entries that were (or would be,
    in dry_run mode) removed. Malformed settings.json leaves the file
    untouched and returns 0 with a warning to stderr. Non-dry-run write
    failures propagate to the lifecycle command so it can report them.
    """
    p = Path(settings_path)
    if not p.exists():
        return 0

    try:
        raw = p.read_text()
        settings = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: {p} could not be read or parsed ({e}); leaving it alone.",
              file=sys.stderr)
        return 0

    if not isinstance(settings, dict):
        print(f"WARN: {p} top-level JSON is not an object; leaving it alone.",
              file=sys.stderr)
        return 0

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        return 0

    removed = 0
    for event_name in list(hooks.keys()):
        matcher_groups = hooks[event_name]
        if not isinstance(matcher_groups, list):
            continue
        kept_groups = []
        for mg in matcher_groups:
            if not isinstance(mg, dict):
                kept_groups.append(mg)
                continue
            entries = mg.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(mg)
                continue
            kept_entries = []
            for h in entries:
                if _is_chronicle_hook_entry(h):
                    removed += 1
                else:
                    kept_entries.append(h)
            if kept_entries:
                new_mg = dict(mg)
                new_mg["hooks"] = kept_entries
                kept_groups.append(new_mg)
            # else: matcher group is now empty — drop it
        if kept_groups:
            hooks[event_name] = kept_groups
        else:
            del hooks[event_name]

    if not hooks:
        settings.pop("hooks", None)
    else:
        settings["hooks"] = hooks

    if removed and not dry_run:
        p.write_text(json.dumps(settings, indent=2) + "\n")

    return removed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python install_hooks.py <settings.json path>")
        sys.exit(1)
    install_hooks(sys.argv[1])
