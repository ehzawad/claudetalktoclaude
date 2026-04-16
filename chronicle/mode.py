"""Processing-mode state.

Two modes:
- "foreground": no daemon. Hooks log events and inject context but never
  summarize. User triggers summarization explicitly via `chronicle process`
  / `chronicle insight` / `chronicle story` / `chronicle rewind --summary`.
  Zero passive token burn.
- "background": launchd/systemd daemon auto-summarizes sessions after a
  quiet window. Hooks respawn the daemon if it's dead.

The config file (~/.chronicle/config.json) is the authoritative source of
truth for mode. Service-file presence (~/Library/LaunchAgents/com.chronicle.daemon.plist
or ~/.config/systemd/user/chronicle-daemon.service) is a managed effect;
mismatches are "drift" reported by `chronicle doctor` — they do not
change behavior.
"""

from __future__ import annotations

import json
import os

from .config import config_file, PROCESSING_MODES, load_config, save_default_config


def get_processing_mode() -> str:
    """Return the current processing mode ("foreground" or "background")."""
    mode = load_config().get("processing_mode", "foreground")
    if mode not in PROCESSING_MODES:
        return "foreground"
    return mode


def is_background_mode() -> bool:
    return get_processing_mode() == "background"


def is_foreground_mode() -> bool:
    return get_processing_mode() == "foreground"


def set_processing_mode(mode: str) -> None:
    """Persist the processing mode to config.json (atomic write)."""
    if mode not in PROCESSING_MODES:
        raise ValueError(
            f"invalid mode {mode!r}; must be one of {PROCESSING_MODES}"
        )
    save_default_config()  # ensures file exists
    cfg = {}
    if config_file().exists():
        try:
            cfg = json.loads(config_file().read_text())
        except (OSError, json.JSONDecodeError):
            cfg = {}
    cfg["processing_mode"] = mode
    tmp = config_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(str(tmp), str(config_file()))
    os.chmod(config_file(), 0o600)
