"""Shared storage operations for chronicle data.

Handles session record writing, chronicle appending, and processed-session
tracking. Used by both daemon.py (real-time processing) and batch.py
(retroactive processing).
"""

import hashlib
import re

from .config import CHRONICLE_DIR, ensure_dirs, project_chronicle_dir
from .summarizer import entry_to_session_markdown


def chronicled_hash(session_id: str, end_time: str) -> str:
    return hashlib.sha256(f"{session_id}:{end_time}".encode()).hexdigest()[:16]


def already_chronicled(session_id: str, end_time: str) -> bool:
    """Check if this session version has already been chronicled."""
    marker_dir = CHRONICLE_DIR / ".processed"
    marker_dir.mkdir(exist_ok=True)
    h = chronicled_hash(session_id, end_time)
    return (marker_dir / h).exists()


def mark_chronicled(session_id: str, end_time: str):
    marker_dir = CHRONICLE_DIR / ".processed"
    marker_dir.mkdir(exist_ok=True)
    h = chronicled_hash(session_id, end_time)
    (marker_dir / h).write_text(f"{session_id}\n{end_time}\n")


def get_attempt_count(session_id: str, end_time: str) -> int:
    """Get the number of failed processing attempts for a session."""
    marker_dir = CHRONICLE_DIR / ".processed"
    h = chronicled_hash(session_id, end_time)
    attempt_file = marker_dir / f"{h}.attempts"
    if attempt_file.exists():
        try:
            return int(attempt_file.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def record_attempt(session_id: str, end_time: str):
    """Increment the failed attempt counter for a session."""
    marker_dir = CHRONICLE_DIR / ".processed"
    marker_dir.mkdir(exist_ok=True)
    h = chronicled_hash(session_id, end_time)
    attempt_file = marker_dir / f"{h}.attempts"
    count = get_attempt_count(session_id, end_time) + 1
    attempt_file.write_text(str(count))


def slugify(text: str, max_len: int = 40) -> str:
    """Turn text into a filename-safe slug."""
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:max_len].rstrip("-")


def session_filename(entry) -> str:
    """Generate filename: 2026-03-31_0611_abc12345_wiring-hooks.md

    Session ID keeps it deterministic (for matching on reprocess).
    Slugified title adds human context.
    """
    ts = entry.start_time[:16] if entry.start_time else "unknown"
    date_part = ts.replace("T", "_").replace(":", "")
    short_id = entry.session_id[:8]
    title_slug = f"_{slugify(entry.title)}" if entry.title else ""
    return f"{date_part}_{short_id}{title_slug}.md"


def write_session_record(entry, slug: str):
    """Write per-session markdown file."""
    ensure_dirs(slug)
    session_dir = project_chronicle_dir(slug) / "sessions"

    # Remove old files for this session (title slug may differ on reprocess)
    short_id = entry.session_id[:8]
    for old in session_dir.glob(f"*_{short_id}*.md"):
        old.unlink()

    session_file = session_dir / session_filename(entry)
    session_file.write_text(entry_to_session_markdown(entry))


def append_to_chronicle(entry, slug: str):
    """Append a concise entry to the cumulative project chronicle.md."""
    ensure_dirs(slug)
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"

    short_id = entry.session_id[:8]
    ts = entry.start_time[:16].replace("T", " ") if entry.start_time else "unknown"
    title = entry.title or f"Session {short_id}"

    # Use a specific marker for duplicate detection instead of substring search
    session_marker = f"<!-- session:{entry.session_id} -->"

    lines = []
    lines.append(f"## {ts} | {title}")
    lines.append(session_marker)
    lines.append("")

    if entry.summary:
        lines.append(entry.summary)
        lines.append("")

    if entry.decisions:
        for d in entry.decisions[:5]:
            what = d.get("what", d) if isinstance(d, dict) else str(d)
            why = d.get("why", "") if isinstance(d, dict) else ""
            lines.append(f"- **{what}**")
            if why:
                lines.append(f"  - {why}")
        if len(entry.decisions) > 5:
            lines.append(f"- ...and {len(entry.decisions) - 5} more decisions")
        lines.append("")

    if entry.open_questions:
        lines.append("Open questions:")
        for q in entry.open_questions[:3]:
            lines.append(f"- {q}")
        lines.append("")

    sf = session_filename(entry)
    lines.append(f"*Full session: [sessions/{sf}](sessions/{sf})*")
    lines.append("")
    lines.append("---")
    lines.append("")

    section = "\n".join(lines)

    if chronicle_file.exists():
        existing = chronicle_file.read_text()
        # Check for duplicate using the specific marker
        if session_marker in existing:
            return
        chronicle_file.write_text(existing + section)
    else:
        project_name = slug.rsplit("-", 1)[-1] if "-" in slug else slug
        header = f"# Chronicle: {project_name}\n\n"
        chronicle_file.write_text(header + section)


def write_chronicle(entry, digest, max_retries: int = 3):
    """Write per-session detail file and append to cumulative chronicle."""
    if entry.is_error:
        record_attempt(digest.session_id, digest.end_time)
        attempts = get_attempt_count(digest.session_id, digest.end_time)
        if attempts >= max_retries:
            print(f"[chronicle] giving up on {digest.session_id[:8]} after {attempts} failed attempts")
            mark_chronicled(digest.session_id, digest.end_time)
        else:
            print(f"[chronicle] transient error for {digest.session_id[:8]} "
                  f"(attempt {attempts}/{max_retries}), will retry later")
        return

    if entry.is_empty:
        print(f"[chronicle] no decisions in {digest.session_id[:8]}")
        mark_chronicled(digest.session_id, digest.end_time)
        return

    write_session_record(entry, digest.project_slug)
    append_to_chronicle(entry, digest.project_slug)
    mark_chronicled(digest.session_id, digest.end_time)
