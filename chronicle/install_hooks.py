"""Configure chronicle hooks in Claude Code settings.json.

Called by install.sh. Merges hooks into existing settings without
overwriting other keys. Also exposes uninstall_hooks() for the
`chronicle uninstall` subcommand.
"""

import json
import os
import sys
from pathlib import Path

CHRONICLE_HOOKS = {
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "chronicle-hook",
                    "statusMessage": "Loading chronicle context...",
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "chronicle-hook", "async": True}],
        }
    ],
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "chronicle-hook", "async": True}],
        }
    ],
    "SessionEnd": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "chronicle-hook", "async": True}],
        }
    ],
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
    # canonical matcher group.
    for event_name, chronicle_matchers in CHRONICLE_HOOKS.items():
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
                if isinstance(entry, dict) and _is_chronicle_hook_command(entry.get("command")):
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


def _is_chronicle_hook_command(cmd) -> bool:
    """True if a hook command invokes chronicle-hook.

    Accepts literal 'chronicle-hook' as well as absolute paths like
    '/Users/ehz/.local/bin/chronicle-hook', and tolerates trailing flags
    ('chronicle-hook --foo'). Splits on whitespace, takes the first token,
    compares basename.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    first = cmd.strip().split(None, 1)[0]
    return os.path.basename(first) == "chronicle-hook"


def uninstall_hooks(settings_path: str, dry_run: bool = False) -> int:
    """Remove chronicle-hook entries from Claude Code settings.json.

    Subtractive: removes individual hook entries whose command invokes
    chronicle-hook. Preserves other hook entries users may have placed in
    the same matcher group. Drops matcher groups that become empty. Drops
    events that become empty. If the whole 'hooks' key becomes empty,
    drops it too.

    Returns the number of chronicle-hook entries that were (or would be,
    in dry_run mode) removed. Never raises; a malformed settings.json
    leaves the file untouched and returns 0 with a warning to stderr.
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
                cmd = (h or {}).get("command") if isinstance(h, dict) else None
                if _is_chronicle_hook_command(cmd):
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
