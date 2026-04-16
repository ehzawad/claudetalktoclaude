"""Unit tests for chronicle.filtering.should_skip."""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    import importlib
    import chronicle.config
    import chronicle.storage
    import chronicle.filtering
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.storage)
    importlib.reload(chronicle.filtering)
    yield
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.storage)
    importlib.reload(chronicle.filtering)


class FakeDigest:
    def __init__(self, session_id="sid", project_slug="-x", user_prompts=None):
        self.session_id = session_id
        self.project_slug = project_slug
        self.user_prompts = user_prompts or []


class FakePrompt:
    def __init__(self, text):
        self.text = text


def test_self_session_detected(isolated):
    from chronicle import filtering
    d = FakeDigest(user_prompts=[FakePrompt(
        "You are writing a high-fidelity engineering chronicle\n\n...")])
    assert filtering.should_skip(d, {}) == "chronicle self-session"


def test_skip_project_matches_substring(isolated):
    from chronicle import filtering
    d = FakeDigest(project_slug="-home-foo-skipme")
    assert filtering.should_skip(d, {"skip_projects": ["skipme"]}) == \
        "project in skip list"


def test_already_chronicled(isolated):
    from chronicle import filtering, storage
    d = FakeDigest()
    storage.mark_succeeded(d.session_id, "", 0.0)
    assert filtering.should_skip(d, {}) == "already chronicled"
    # force bypasses
    assert filtering.should_skip(d, {}, force=True) is None


def test_terminal_failure_skipped_by_default(isolated):
    from chronicle import filtering, storage
    d = FakeDigest(session_id="fail1")
    storage.record_failed_attempt(
        d.session_id, error_kind="transient",
        error_message="x", terminal=True,
    )
    assert filtering.should_skip(d, {}) == "terminal failure"
    # retry_failed bypasses
    assert filtering.should_skip(d, {}, retry_failed=True) is None
    # force also bypasses
    assert filtering.should_skip(d, {}, force=True) is None


def test_non_terminal_failure_not_skipped(isolated):
    """Retriable (non-terminal) failures should be processed, not skipped."""
    from chronicle import filtering, storage
    d = FakeDigest(session_id="partial")
    storage.record_failed_attempt(
        d.session_id, error_kind="transient",
        error_message="x", terminal=False,
    )
    assert filtering.should_skip(d, {}) is None


def test_clean_session_passes(isolated):
    from chronicle import filtering
    d = FakeDigest()
    assert filtering.should_skip(d, {}) is None
