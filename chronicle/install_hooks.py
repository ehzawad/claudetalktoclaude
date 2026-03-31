"""Configure chronicle hooks in Claude Code settings.json.

Called by install.sh. Merges hooks into existing settings without
overwriting other keys.
"""

import json
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


def _has_chronicle_hook(matcher_group: dict) -> bool:
    """Check if a matcher group contains a chronicle-hook command."""
    for hook in matcher_group.get("hooks", []):
        if hook.get("command") == "chronicle-hook":
            return True
    return False


def install_hooks(settings_path: str):
    path = Path(settings_path)

    if path.exists():
        settings = json.loads(path.read_text())
    else:
        settings = {}

    hooks = settings.get("hooks", {})

    # Merge Chronicle hooks into existing hooks without replacing user entries.
    # For each event, append Chronicle's matcher group to the existing list
    # (if not already present), preserving any user-defined hooks.
    for event_name, chronicle_matchers in CHRONICLE_HOOKS.items():
        existing = hooks.get(event_name, [])
        # Remove any existing chronicle-hook entries (for idempotent reinstall)
        existing = [mg for mg in existing if not _has_chronicle_hook(mg)]
        # Append Chronicle's matcher groups
        hooks[event_name] = existing + chronicle_matchers

    settings["hooks"] = hooks

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Configured hooks in {path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python install_hooks.py <settings.json path>")
        sys.exit(1)
    install_hooks(sys.argv[1])
