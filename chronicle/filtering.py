"""Shared session filtering logic for daemon and batch processing."""

from .storage import already_chronicled

_SELF_SESSION_MARKERS = (
    "You are a Decision Chronicler",
    "You are writing a high-fidelity engineering chronicle",
)


def should_skip(digest, config: dict, force: bool = False) -> str | None:
    """Check if a session should be skipped. Returns reason string or None.

    Used by both the daemon (real-time) and batch (retroactive) pipelines
    to apply consistent filtering.
    """
    min_turns = config.get("min_turns_to_chronicle", 1)
    if digest.total_turns < min_turns:
        return f"only {digest.total_turns} turns"

    if not digest.user_prompts:
        return "no user prompts"

    if any(digest.user_prompts[0].text.startswith(m) for m in _SELF_SESSION_MARKERS):
        return "chronicle self-session"

    skip_projects = config.get("skip_projects", [])
    if any(sp in digest.project_slug for sp in skip_projects):
        return "project in skip list"

    if not force and already_chronicled(digest.session_id, digest.end_time):
        return "already chronicled"

    return None
