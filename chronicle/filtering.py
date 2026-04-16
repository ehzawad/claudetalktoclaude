"""Session filtering logic for daemon and batch processing.

Returns a reason-string for skipping or None to process. The reason string
is used both for logging and to drive CLI UX (e.g., "already chronicled"
vs "terminal failure — use --retry-failed").
"""

from .storage import is_succeeded, is_terminal_failure

_SELF_SESSION_MARKERS = (
    "You are a Decision Chronicler",
    "You are writing a high-fidelity engineering chronicle",
)


def should_skip(digest, config: dict, *, force: bool = False,
                retry_failed: bool = False) -> str | None:
    """Check if a session should be skipped. Returns reason or None.

    Reason strings:
      - "chronicle self-session" — summarizer calling itself (infinite-loop guard)
      - "project in skip list"   — config.skip_projects match
      - "already chronicled"     — success marker present (bypass with force)
      - "terminal failure"       — failure marker w/ terminal=true (bypass with retry_failed or force)
      - None                     — process this session
    """
    if digest.user_prompts and any(
        digest.user_prompts[0].text.startswith(m) for m in _SELF_SESSION_MARKERS
    ):
        return "chronicle self-session"

    skip_projects = config.get("skip_projects", [])
    if any(sp in digest.project_slug for sp in skip_projects):
        return "project in skip list"

    if not force and is_succeeded(digest.session_id):
        return "already chronicled"

    if not (force or retry_failed) and is_terminal_failure(digest.session_id):
        return "terminal failure"

    return None
