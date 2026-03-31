"""Tests for storage.py — chronicle writing, duplicate detection, retry tracking."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from chronicle.storage import (
    append_to_chronicle,
    chronicled_hash,
    already_chronicled,
    mark_chronicled,
    get_attempt_count,
    record_attempt,
    slugify,
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

                    class FakeEntry:
                        session_id = "abc12345deadbeef"
                        start_time = "2026-04-01T00:00:00Z"
                        title = "Test session"
                        summary = "A test"
                        decisions = []
                        open_questions = []
                        is_error = False
                        is_empty = False

                    entry = FakeEntry()

                    # First append should write
                    append_to_chronicle(entry, "test-slug")
                    chronicle = (Path(tmpdir) / "chronicle.md").read_text()
                    assert "<!-- session:abc12345deadbeef -->" in chronicle
                    assert "Test session" in chronicle

                    # Second append should be skipped (duplicate)
                    append_to_chronicle(entry, "test-slug")
                    chronicle2 = (Path(tmpdir) / "chronicle.md").read_text()
                    assert chronicle2.count("Test session") == 1  # not duplicated

    def test_different_sessions_both_appended(self):
        """Two different session IDs should both appear."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    (Path(tmpdir) / "sessions").mkdir()

                    class FakeEntry:
                        def __init__(self, sid, title):
                            self.session_id = sid
                            self.start_time = "2026-04-01T00:00:00Z"
                            self.title = title
                            self.summary = ""
                            self.decisions = []
                            self.open_questions = []
                            self.is_error = False
                            self.is_empty = False

                    append_to_chronicle(FakeEntry("aaa11111", "First"), "test-slug")
                    append_to_chronicle(FakeEntry("bbb22222", "Second"), "test-slug")

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
