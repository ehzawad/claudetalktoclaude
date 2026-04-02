"""Tests for storage.py — chronicle writing, duplicate detection, retry tracking, timeline table."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from chronicle.config import load_recent_titles
from chronicle.storage import (
    append_to_chronicle,
    _timeline_row,
    _retrofit_timeline,
    _TIMELINE_HEADER,
    _TIMELINE_SEP,
    _TIMELINE_END,
    chronicled_hash,
    already_chronicled,
    mark_chronicled,
    get_attempt_count,
    record_attempt,
    slugify,
)
from chronicle.summarizer import ChronicleEntry


def _fake_entry(sid="abc12345deadbeef", title="Test session", summary="A test",
                decisions=None, open_questions=None, start_time="2026-04-01T00:00:00Z"):
    """Create a ChronicleEntry with defaults for testing."""
    return ChronicleEntry(
        session_id=sid, project_path="/test", project_slug="test-slug",
        start_time=start_time, end_time=start_time, git_branch="main",
        user_prompts=[], title=title, summary=summary,
        decisions=decisions or [], open_questions=open_questions or [],
    )


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("Fix bug #123!") == "fix-bug-123"

    def test_max_length(self):
        result = slugify("a" * 100, max_len=10)
        assert len(result) <= 10

    def test_empty(self):
        assert slugify("") == ""

    def test_consecutive_separators(self):
        assert slugify("hello---world") == "hello-world"


class TestDuplicateDetection:
    def test_append_uses_session_marker(self):
        """append_to_chronicle should use <!-- session:ID --> marker for dedup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    entry = _fake_entry()

                    append_to_chronicle(entry, "test-slug")
                    chronicle = (Path(tmpdir) / "chronicle.md").read_text()
                    assert "<!-- session:abc12345deadbeef -->" in chronicle
                    assert "Test session" in chronicle

                    # Second append replaces (not duplicates)
                    append_to_chronicle(entry, "test-slug")
                    chronicle2 = (Path(tmpdir) / "chronicle.md").read_text()
                    assert chronicle2.count("<!-- session:abc12345deadbeef -->") == 1

    def test_different_sessions_both_appended(self):
        """Two different session IDs should both appear."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    append_to_chronicle(_fake_entry("aaa11111", "First"), "test-slug")
                    append_to_chronicle(_fake_entry("bbb22222", "Second"), "test-slug")

                    chronicle = (Path(tmpdir) / "chronicle.md").read_text()
                    assert "First" in chronicle
                    assert "Second" in chronicle


class TestRetryTracking:
    def test_attempt_count_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.CHRONICLE_DIR", Path(tmpdir)):
                (Path(tmpdir) / ".processed").mkdir()
                assert get_attempt_count("test-session", "2026-04-01") == 0

    def test_record_increments_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.CHRONICLE_DIR", Path(tmpdir)):
                (Path(tmpdir) / ".processed").mkdir()
                record_attempt("test-session", "2026-04-01")
                assert get_attempt_count("test-session", "2026-04-01") == 1
                record_attempt("test-session", "2026-04-01")
                assert get_attempt_count("test-session", "2026-04-01") == 2

    def test_chronicled_hash_deterministic(self):
        h1 = chronicled_hash("abc", "2026-04-01")
        h2 = chronicled_hash("abc", "2026-04-01")
        assert h1 == h2
        assert len(h1) == 16

    def test_chronicled_hash_different_inputs(self):
        h1 = chronicled_hash("abc", "2026-04-01")
        h2 = chronicled_hash("def", "2026-04-01")
        assert h1 != h2


class TestChronicledMarkers:
    def test_mark_and_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.CHRONICLE_DIR", Path(tmpdir)):
                assert already_chronicled("test", "2026-04-01") is False
                mark_chronicled("test", "2026-04-01")
                assert already_chronicled("test", "2026-04-01") is True


class TestTimelineRow:
    def test_basic_row(self):
        e = _fake_entry(title="Fix the auth bug", summary="Fixed token refresh.",
                        decisions=[{"what": "Use JWT"}, {"what": "Drop sessions"}],
                        start_time="2026-04-01T10:30:00Z")
        row = _timeline_row(e, "2026-04-01_1030_abc12345_fix-the-auth-bug.md")
        assert "| 2026-04-01 10:30 |" in row
        assert "[Fix the auth bug]" in row
        assert "| 2 |" in row
        assert "Fixed token refresh." in row

    def test_long_title_truncated(self):
        e = _fake_entry(title="A" * 100, summary="")
        row = _timeline_row(e, "test.md")
        assert "..." in row

    def test_pipe_in_summary_escaped(self):
        e = _fake_entry(title="Test", summary="Used auth|token pattern")
        row = _timeline_row(e, "test.md")
        assert "auth/token" in row


class TestTimelineTable:
    def test_new_chronicle_has_timeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    append_to_chronicle(_fake_entry("aaa", "First"), "slug")
                    content = (Path(tmpdir) / "chronicle.md").read_text()
                    assert _TIMELINE_HEADER in content
                    assert _TIMELINE_SEP in content
                    assert _TIMELINE_END in content
                    assert "[First]" in content

    def test_second_append_inserts_row_at_top(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    append_to_chronicle(_fake_entry("aaa", "First"), "slug")
                    append_to_chronicle(_fake_entry("bbb", "Second"), "slug")
                    content = (Path(tmpdir) / "chronicle.md").read_text()
                    assert "[First]" in content
                    assert "[Second]" in content
                    # Second row should appear before First in the table
                    second_pos = content.index("[Second]")
                    first_pos = content.index("[First]")
                    assert second_pos < first_pos

    def test_detail_sections_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    append_to_chronicle(
                        _fake_entry("aaa", "First", summary="Detailed work"),
                        "slug",
                    )
                    content = (Path(tmpdir) / "chronicle.md").read_text()
                    assert "# First" in content
                    assert "Detailed work" in content


class TestRetrofitTimeline:
    def test_adds_table_to_old_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chronicle_file = Path(tmpdir) / "chronicle.md"
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()

            old_content = """# Chronicle: testproject

## 2026-04-01 10:00 | Old session one
<!-- session:aaa11111-aaaa-aaaa-aaaa-aaaaaaaaaaaa -->

Summary of first session.

- **Decision alpha**

*Full session: [sessions/old1.md](sessions/old1.md)*

---

## 2026-04-01 14:00 | Old session two
<!-- session:bbb22222-bbbb-bbbb-bbbb-bbbbbbbbbbbb -->

Summary of second session.

- **Decision beta**
- **Decision gamma**

*Full session: [sessions/old2.md](sessions/old2.md)*

---
"""
            chronicle_file.write_text(old_content)
            _retrofit_timeline(chronicle_file, old_content)

            content = chronicle_file.read_text()
            assert _TIMELINE_HEADER in content
            assert _TIMELINE_SEP in content
            assert _TIMELINE_END in content
            # Both old sessions appear in the table
            assert "Old session one" in content
            assert "Old session two" in content
            # Detail sections still present
            assert "<!-- session:aaa11111" in content
            assert "<!-- session:bbb22222" in content


class TestLoadRecentTitles:
    def test_reads_titles_from_session_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "test-slug" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "2026-04-01_session1.md").write_text("# Auth refactor\n\nContent")
            (sessions_dir / "2026-04-02_session2.md").write_text("# DB migration\n\nContent")

            with patch("chronicle.config.PROJECTS_DIR", Path(tmpdir)):
                titles = load_recent_titles("test-slug")
                assert len(titles) == 2
                assert "DB migration" in titles  # newest first
                assert "Auth refactor" in titles

    def test_empty_sessions_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "test-slug" / "sessions"
            sessions_dir.mkdir(parents=True)
            with patch("chronicle.config.PROJECTS_DIR", Path(tmpdir)):
                titles = load_recent_titles("test-slug")
                assert titles == []

    def test_no_sessions_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.config.PROJECTS_DIR", Path(tmpdir)):
                titles = load_recent_titles("nonexistent-slug")
                assert titles == []

    def test_max_entries_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "test-slug" / "sessions"
            sessions_dir.mkdir(parents=True)
            for i in range(20):
                (sessions_dir / f"2026-04-{i:02d}_s{i}.md").write_text(f"# Session {i}\n")
            with patch("chronicle.config.PROJECTS_DIR", Path(tmpdir)):
                titles = load_recent_titles("test-slug", max_entries=5)
                assert len(titles) == 5


class TestSessionFilename:
    def test_generates_deterministic_name(self):
        from chronicle.storage import session_filename
        e1 = _fake_entry("abc12345", "My title", start_time="2026-04-01T10:30:00Z")
        e2 = _fake_entry("abc12345", "My title", start_time="2026-04-01T10:30:00Z")
        assert session_filename(e1) == session_filename(e2)

    def test_includes_session_id(self):
        from chronicle.storage import session_filename
        e = _fake_entry("deadbeef12345678", "Test")
        assert "deadbeef" in session_filename(e)

    def test_slugifies_title(self):
        from chronicle.storage import session_filename
        e = _fake_entry(title="Fix The Auth Bug!")
        name = session_filename(e)
        assert "fix-the-auth-bug" in name


class TestWriteSessionRecord:
    def test_writes_md_file(self):
        from chronicle.storage import write_session_record, session_filename
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    entry = _fake_entry("aaa11111", "Test write", summary="Content here")
                    write_session_record(entry, "slug")
                    sf = session_filename(entry)
                    written = (Path(tmpdir) / "sessions" / sf).read_text()
                    assert "Test write" in written
                    assert "Content here" in written

    def test_replaces_old_file_on_rewrite(self):
        from chronicle.storage import write_session_record, session_filename
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()
                    e1 = _fake_entry("aaa11111", "Old title")
                    write_session_record(e1, "slug")
                    e2 = _fake_entry("aaa11111", "New title")
                    write_session_record(e2, "slug")
                    # Old file should be gone, new one present
                    files = list((Path(tmpdir) / "sessions").glob("*_aaa11111*.md"))
                    assert len(files) == 1
                    assert "New title" in files[0].read_text()


class TestDemoteHeadings:
    def test_demotes_h1_to_h2(self):
        from chronicle.storage import _demote_headings
        result = _demote_headings("# Title\n\n## Section\n\ntext")
        assert result.startswith("## Title")
        assert "### Section" in result

    def test_preserves_code_blocks(self):
        from chronicle.storage import _demote_headings
        md = "# Title\n\n```\n# this is a comment\n```\n\n## Section"
        result = _demote_headings(md)
        assert "# this is a comment" in result  # NOT demoted
        assert "## Title" in result  # demoted
        assert "### Section" in result  # demoted


class TestRemoveSessionEntry:
    def test_removes_entry_and_timeline_row(self):
        from chronicle.storage import _remove_session_entry
        content = """# Chronicle: test

| Date | Session | Decisions | Summary |
|------|---------|-----------|---------|
| 2026-04-01 | [First](sessions/aaa11111.md) | 1 | Sum |
<!-- /timeline -->

## First
<!-- session:aaa11111-full-uuid -->

Some content.

---

## Second
<!-- session:bbb22222-full-uuid -->

Other content.

---
"""
        result = _remove_session_entry(content, "<!-- session:aaa11111-full-uuid -->")
        assert "aaa11111" not in result
        assert "Second" in result
        assert "bbb22222" in result
