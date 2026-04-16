"""Unit tests for chronicle.storage (marker state + failure record)."""
from __future__ import annotations

import hashlib
import json

import pytest


@pytest.fixture
def isolated_chronicle(tmp_path, monkeypatch):
    """Per-test isolated HOME with empty ~/.chronicle and ~/.claude/projects."""
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    import importlib
    import chronicle.config
    import chronicle.storage
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.storage)
    yield fake_home / ".chronicle", fake_home / ".claude" / "projects"
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.storage)


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:16]


class TestSuccessMarkers:
    def test_mark_writes_and_clears_failure(self, isolated_chronicle):
        chronicle_dir, _ = isolated_chronicle
        from chronicle import storage
        # Pre-existing failure record
        storage.record_failed_attempt(
            "sid1", error_kind="transient", error_message="x", terminal=True,
        )
        assert storage.is_terminal_failure("sid1")
        # Success clears it
        storage.mark_succeeded("sid1", "2026-04-16T00:00:00Z", cost_usd=0.02)
        assert storage.is_succeeded("sid1")
        assert not storage.is_terminal_failure("sid1")
        assert storage.get_failed("sid1") is None

    def test_is_succeeded(self, isolated_chronicle):
        from chronicle import storage
        assert not storage.is_succeeded("nope")
        storage.mark_succeeded("yes", "", 0.0)
        assert storage.is_succeeded("yes")


class TestFailureRecord:
    def test_first_transient_attempt_not_terminal(self, isolated_chronicle):
        from chronicle import storage
        n = storage.record_failed_attempt(
            "sid", error_kind="transient", error_message="x", terminal=False,
        )
        assert n == 1
        assert storage.get_attempt_count("sid") == 1
        assert not storage.is_terminal_failure("sid")

    def test_attempts_accumulate(self, isolated_chronicle):
        from chronicle import storage
        storage.record_failed_attempt("s", error_kind="transient",
                                      error_message="a", terminal=False)
        storage.record_failed_attempt("s", error_kind="transient",
                                      error_message="b", terminal=False)
        n = storage.record_failed_attempt("s", error_kind="transient",
                                          error_message="c", terminal=True)
        assert n == 3
        assert storage.is_terminal_failure("s")
        rec = storage.get_failed("s")
        assert rec["attempts"] == 3
        assert rec["last_error_message"] == "c"

    def test_clear_failed(self, isolated_chronicle):
        from chronicle import storage
        storage.record_failed_attempt("x", error_kind="transient",
                                      error_message="y", terminal=True)
        storage.clear_failed("x")
        assert storage.get_failed("x") is None

    def test_list_failed_filters(self, isolated_chronicle):
        from chronicle import storage
        storage.record_failed_attempt("a", error_kind="transient",
                                      error_message="", terminal=False)
        storage.record_failed_attempt("b", error_kind="transient",
                                      error_message="", terminal=True)
        all_ = storage.list_failed()
        term = storage.list_failed(terminal_only=True)
        assert len(all_) == 2
        assert len(term) == 1
        assert term[0]["session_id"] == "b"


class TestWriteChronicleRetryAccounting:
    def _make_digest(self):
        # Minimal SessionDigest-like object for write_chronicle
        class FakeDigest:
            session_id = "abc-123"
            project_slug = "-tmp-x"
            end_time = ""
        return FakeDigest()

    def _make_entry(self, *, is_error=False, error_kind="", error_message=""):
        class FakeEntry:
            pass
        e = FakeEntry()
        e.is_error = is_error
        e.is_empty = False
        e.error_kind = error_kind
        e.error_message = error_message
        e.total_cost_usd = 0.0
        e.session_id = "abc-123"
        return e

    def test_infra_error_does_not_count(self, isolated_chronicle):
        from chronicle import storage
        digest = self._make_digest()
        entry = self._make_entry(is_error=True, error_kind="infra",
                                 error_message="binary missing")
        storage.write_chronicle(entry, digest, max_retries=3)
        # No failure record, no attempt counter advance
        assert storage.get_failed(digest.session_id) is None
        assert storage.get_attempt_count(digest.session_id) == 0

    def test_transient_error_counts_up_to_max(self, isolated_chronicle):
        from chronicle import storage
        digest = self._make_digest()
        entry = self._make_entry(is_error=True, error_kind="transient",
                                 error_message="boom")
        storage.write_chronicle(entry, digest, max_retries=3)
        assert storage.get_attempt_count(digest.session_id) == 1
        assert not storage.is_terminal_failure(digest.session_id)
        storage.write_chronicle(entry, digest, max_retries=3)
        assert storage.get_attempt_count(digest.session_id) == 2
        assert not storage.is_terminal_failure(digest.session_id)
        storage.write_chronicle(entry, digest, max_retries=3)
        assert storage.get_attempt_count(digest.session_id) == 3
        assert storage.is_terminal_failure(digest.session_id)
