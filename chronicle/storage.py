"""Shared storage operations for chronicle data.

Handles session record writing, chronicle appending, and processed-session
tracking. Used by both daemon.py (real-time processing) and batch.py
(retroactive processing).
"""

import hashlib
import os
import re

from .config import CHRONICLE_DIR, ensure_dirs, project_chronicle_dir
from .summarizer import entry_to_session_markdown


def _atomic_write(path, content: str):
    """Write content atomically via temp file + os.replace."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(str(tmp), str(path))


def chronicled_hash(session_id: str, end_time: str = "") -> str:
    # Hash on session_id only — JSONL files grow after processing (e.g.
    # SessionEnd appended after Stop), shifting end_time and invalidating
    # the old session_id:end_time hash.  Use --force to reprocess.
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def already_chronicled(session_id: str, end_time: str) -> bool:
    """Check if this session has already been chronicled."""
    marker_dir = CHRONICLE_DIR / ".processed"
    marker_dir.mkdir(exist_ok=True)
    h = chronicled_hash(session_id, end_time)
    return (marker_dir / h).exists()


def mark_chronicled(session_id: str, end_time: str, cost_usd: float = 0.0):
    marker_dir = CHRONICLE_DIR / ".processed"
    marker_dir.mkdir(exist_ok=True)
    h = chronicled_hash(session_id, end_time)
    (marker_dir / h).write_text(f"{session_id}\n{end_time}\n{cost_usd:.4f}\n")


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


def unmark_chronicled(session_id: str):
    """Remove the processed marker for a session so it can be reprocessed.

    Tries exact hash first. If that fails (e.g. short ID vs full UUID mismatch),
    scans marker files by content to find the right one.
    """
    marker_dir = CHRONICLE_DIR / ".processed"
    if not marker_dir.exists():
        return

    # Try exact hash
    h = chronicled_hash(session_id)
    marker = marker_dir / h
    if marker.exists():
        marker.unlink()
        attempt = marker_dir / f"{h}.attempts"
        if attempt.exists():
            attempt.unlink()
        return

    # Fallback: scan marker files by content (handles short ID → full UUID)
    for marker in marker_dir.glob("[0-9a-f]*"):
        if marker.suffix == ".attempts":
            continue
        try:
            content = marker.read_text()
            if content.startswith(session_id):
                marker.unlink()
                attempt = marker.with_suffix(".attempts")
                if attempt.exists():
                    attempt.unlink()
                return
        except OSError:
            continue


def delete_session(session_path, slug: str):
    """Delete a session .md file, its chronicle.md entry, and its processed marker.

    Does NOT touch ~/.claude/ — only chronicle's own data.
    """
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"

    # Extract short session_id from the markdown metadata
    content = session_path.read_text()
    sid_match = re.search(r"\*\*Session\*\*:\s*(\w+)", content)
    short_id = sid_match.group(1) if sid_match else session_path.stem[:8]

    # Try to find the full UUID from chronicle.md's <!-- session:UUID --> marker
    full_id = short_id
    if chronicle_file.exists():
        chronicle = chronicle_file.read_text()
        # The marker has the full UUID: <!-- session:xxxxxxxx-xxxx-... -->
        full_match = re.search(rf"<!-- session:({re.escape(short_id)}[a-f0-9-]*)", chronicle)
        if full_match:
            full_id = full_match.group(1)

        session_marker = f"<!-- session:{full_id}"
        for line in chronicle.split("\n"):
            if session_marker in line:
                session_marker = line.strip()
                break
        if session_marker in chronicle:
            chronicle = _remove_session_entry(chronicle, session_marker)
            _atomic_write(chronicle_file, chronicle)

    # Remove session .md file
    session_path.unlink()

    # Remove processed marker — try full UUID first, then short ID
    unmark_chronicled(full_id)
    if full_id != short_id:
        unmark_chronicled(short_id)

    # Rebuild prompts section
    rebuild_prompts_section(slug)


def write_session_record(entry, slug: str):
    """Write per-session markdown file."""
    ensure_dirs(slug)
    session_dir = project_chronicle_dir(slug) / "sessions"

    # Remove old files for this session (title slug may differ on reprocess)
    short_id = entry.session_id[:8]
    for old in session_dir.glob(f"*_{short_id}*.md"):
        old.unlink()

    session_file = session_dir / session_filename(entry)
    _atomic_write(session_file, entry_to_session_markdown(entry))


def _remove_session_entry(content: str, session_marker: str) -> str:
    """Remove an existing session's timeline row + detail section from chronicle.md."""
    marker_idx = content.index(session_marker)

    # Walk backwards from the marker to find the nearest heading (# or ##)
    search_region = content[max(0, marker_idx - 300):marker_idx]
    heading_offset = -1
    for prefix in ("\n# ", "\n## "):
        pos = search_region.rfind(prefix)
        if pos >= 0:
            heading_offset = max(heading_offset, pos)
    if heading_offset >= 0:
        heading_start = max(0, marker_idx - 300) + heading_offset + 1  # +1 skip \n
    else:
        heading_start = marker_idx

    # Find the LAST \n---\n within this session's section, bounded by the
    # next session marker. Using rfind instead of find avoids stopping at
    # the internal --- before <details> and orphaning the prompts block.
    after_marker = marker_idx + len(session_marker)
    next_session = content.find("<!-- session:", after_marker)
    search_bound = next_session if next_session >= 0 else len(content)

    separator = "\n---\n"
    sep_idx = content.rfind(separator, marker_idx, search_bound)
    if sep_idx >= 0:
        section_end = sep_idx + len(separator)
    else:
        section_end = search_bound

    content = content[:heading_start] + content[section_end:]

    # Remove stale timeline table row
    sid = session_marker.split(":")[1].split(" ")[0]
    short_id = sid[:8]
    lines = content.split("\n")
    cleaned = [l for l in lines if not (l.startswith("|") and short_id in l and "](sessions/" in l)]
    return "\n".join(cleaned)


def _demote_headings(md: str) -> str:
    """Demote markdown headings by one level (# → ##, ## → ###, etc.).

    Only demotes headings that are NOT inside fenced code blocks.
    """
    lines = md.split("\n")
    result = []
    in_code_block = False
    for line in lines:
        if line.startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block and line.startswith("#"):
            line = "#" + line
        result.append(line)
    return "\n".join(result)


_PROMPTS_MARKER = "<!-- prompts -->"


def rebuild_prompts_section(slug: str):
    """Rebuild the combined chronological prompts section at the end of chronicle.md."""
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"
    if not chronicle_file.exists():
        return

    sessions_dir = project_chronicle_dir(slug) / "sessions"
    if not sessions_dir.exists():
        return

    # Collect all prompts from session files
    all_prompts = []
    for md_file in sorted(sessions_dir.glob("*.md")):
        content = md_file.read_text()
        # Extract session title
        title_match = re.match(r"^# (.+)", content)
        session_title = title_match.group(1) if title_match else md_file.stem

        # Extract prompts from the <details> section
        details_match = re.search(
            r"<details><summary>User prompts \(verbatim\)</summary>\s*\n(.*?)</details>",
            content, re.DOTALL
        )
        if not details_match:
            continue

        prompts_text = details_match.group(1)
        # Parse individual prompts: **Prompt N** (timestamp):
        for m in re.finditer(
            r"\*\*Prompt (\d+)\*\* \(([^)]*)\):\s*\n((?:> .+\n?)+)",
            prompts_text
        ):
            num, ts, quoted = m.group(1), m.group(2), m.group(3)
            text = "\n".join(line[2:] for line in quoted.strip().split("\n"))
            all_prompts.append((ts, session_title, int(num), text))

    if not all_prompts:
        # Remove stale prompts section if one exists
        content = chronicle_file.read_text()
        if _PROMPTS_MARKER in content:
            marker_idx = content.index(_PROMPTS_MARKER)
            section_start = content.rfind("\n\n", 0, marker_idx)
            if section_start == -1:
                section_start = marker_idx
            _atomic_write(chronicle_file, content[:section_start].rstrip() + "\n")
        return

    # Sort by timestamp
    all_prompts.sort(key=lambda x: x[0])

    # Build the prompts section
    lines = [
        "",
        _PROMPTS_MARKER,
        "",
        "## All User Prompts (Chronological)",
        "",
    ]
    current_session = None
    for ts, session_title, num, text in all_prompts:
        if session_title != current_session:
            current_session = session_title
            lines.append(f"### {session_title}")
            lines.append("")
        lines.append(f"**Prompt {num}** ({ts}):")
        for pline in text.split("\n"):
            lines.append(f"> {pline}")
        lines.append("")

    prompts_section = "\n".join(lines)

    # Replace or append the prompts section
    content = chronicle_file.read_text()
    if _PROMPTS_MARKER in content:
        marker_idx = content.index(_PROMPTS_MARKER)
        # Find the start (walk back to find blank line before marker)
        section_start = content.rfind("\n\n", 0, marker_idx)
        if section_start == -1:
            section_start = marker_idx
        content = content[:section_start] + prompts_section
    else:
        content = content.rstrip() + "\n" + prompts_section

    _atomic_write(chronicle_file, content + "\n")


_TIMELINE_HEADER = "| Date | Session | Decisions | Summary |"
_TIMELINE_SEP = "|------|---------|-----------|---------|"
_TIMELINE_END = "<!-- /timeline -->"
_DETAIL_START = "<!-- details -->"


def _timeline_row(entry, sf: str) -> str:
    """Build one markdown table row for the timeline."""
    ts = entry.start_time[:16].replace("T", " ") if entry.start_time else "unknown"
    title = entry.title or f"Session {entry.session_id[:8]}"
    # Truncate title for table readability
    if len(title) > 60:
        title = title[:57] + "..."
    n_decisions = len(entry.decisions) if entry.decisions else 0
    summary = (entry.summary or "")[:100].replace("\n", " ").replace("|", "/")
    if entry.summary and len(entry.summary) > 100:
        summary += "..."
    return f"| {ts} | [{title}](sessions/{sf}) | {n_decisions} | {summary} |"


def append_to_chronicle(entry, slug: str):
    """Append to chronicle.md: insert a timeline table row + a detail section."""
    ensure_dirs(slug)
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"

    short_id = entry.session_id[:8]
    ts = entry.start_time[:16].replace("T", " ") if entry.start_time else "unknown"
    title = entry.title or f"Session {short_id}"
    sf = session_filename(entry)

    # Use a specific marker for duplicate detection instead of substring search
    session_marker = f"<!-- session:{entry.session_id} -->"

    # Full session content — demote headings so # Chronicle stays h1
    full_md = entry_to_session_markdown(entry)
    full_md = _demote_headings(full_md)
    # Inject session marker after the first heading for dedup
    first_newline = full_md.index("\n")
    full_md = full_md[:first_newline + 1] + session_marker + "\n" + full_md[first_newline + 1:]
    detail_section = full_md + "\n---\n\n"
    table_row = _timeline_row(entry, sf)

    if chronicle_file.exists():
        existing = chronicle_file.read_text()
        # If session already exists, remove old entry so we can replace it
        if session_marker in existing:
            existing = _remove_session_entry(existing, session_marker)

        if _TIMELINE_END in existing:
            # Insert row into existing timeline (after separator, before end marker)
            # Rows are newest-first, so insert right after the separator line
            sep_idx = existing.index(_TIMELINE_SEP)
            after_sep = existing.index("\n", sep_idx) + 1
            existing = existing[:after_sep] + table_row + "\n" + existing[after_sep:]
            # Append detail section at the end
            _atomic_write(chronicle_file, existing + detail_section)
        else:
            # Old-format chronicle.md — retrofit a timeline table at the top
            _retrofit_timeline(chronicle_file, existing)
            # Re-read and insert normally
            existing = chronicle_file.read_text()
            sep_idx = existing.index(_TIMELINE_SEP)
            after_sep = existing.index("\n", sep_idx) + 1
            existing = existing[:after_sep] + table_row + "\n" + existing[after_sep:]
            _atomic_write(chronicle_file, existing + detail_section)
    else:
        project_name = slug.rsplit("-", 1)[-1] if "-" in slug else slug
        header = f"# Chronicle: {project_name}\n\n"
        timeline = f"{_TIMELINE_HEADER}\n{_TIMELINE_SEP}\n{table_row}\n{_TIMELINE_END}\n\n{_DETAIL_START}\n\n"
        _atomic_write(chronicle_file, header + timeline + detail_section)


def _retrofit_timeline(chronicle_file, existing: str):
    """Add a timeline table to an existing old-format chronicle.md."""
    rows = []
    # Find all existing ## sections and extract their data
    for match in re.finditer(
        r"^## (.+?) \| (.+)\n<!-- session:([a-f0-9-]+) -->",
        existing, re.MULTILINE
    ):
        ts, section_title, session_id = match.group(1), match.group(2), match.group(3)
        # Count decisions (bullet points starting with "- **")
        # Find the section boundary (next ## or end of string)
        start = match.end()
        next_section = re.search(r"^## ", existing[start:], re.MULTILINE)
        section_text = existing[start:start + next_section.start()] if next_section else existing[start:]
        n_decisions = len(re.findall(r"^- \*\*", section_text, re.MULTILINE))

        # Find session file link
        sf_match = re.search(r"\[sessions/(.+?\.md)\]", section_text)
        sf = sf_match.group(1) if sf_match else ""

        # Extract summary (first paragraph after the marker)
        summary_match = re.search(r"\n\n(.+?)(?:\n\n|\Z)", section_text, re.DOTALL)
        summary = ""
        if summary_match:
            summary = summary_match.group(1).strip()[:100].replace("\n", " ").replace("|", "/")
            if len(summary_match.group(1).strip()) > 100:
                summary += "..."

        title = section_title.strip()
        if len(title) > 60:
            title = title[:57] + "..."
        if sf:
            row = f"| {ts} | [{title}](sessions/{sf}) | {n_decisions} | {summary} |"
        else:
            row = f"| {ts} | {title} | {n_decisions} | {summary} |"
        rows.append(row)

    # Find where the header ends (after "# Chronicle: ..." line)
    header_end = existing.index("\n", existing.index("# ")) + 1
    header = existing[:header_end]
    body = existing[header_end:].lstrip("\n")

    timeline = f"\n{_TIMELINE_HEADER}\n{_TIMELINE_SEP}\n"
    timeline += "\n".join(rows) + "\n"
    timeline += f"{_TIMELINE_END}\n\n{_DETAIL_START}\n\n"

    _atomic_write(chronicle_file, header + timeline + body)


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
        # Still write a record — every session appears in rewind
        entry.title = entry.title or f"Session {digest.session_id[:8]}"
        entry.summary = entry.summary or "(No meaningful decisions recorded)"

    write_session_record(entry, digest.project_slug)
    append_to_chronicle(entry, digest.project_slug)
    rebuild_prompts_section(digest.project_slug)
    mark_chronicled(digest.session_id, digest.end_time,
                    cost_usd=getattr(entry, "total_cost_usd", 0.0))
