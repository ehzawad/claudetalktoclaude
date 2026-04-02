"""Tests for filtering.py — session skip logic."""

from unittest.mock import patch
from dataclasses import dataclass, field

from chronicle.filtering import should_skip


@dataclass
class FakePrompt:
    text: str = "Fix the auth bug"
    timestamp: str = ""
    uuid: str = ""


@dataclass
class FakeDigest:
    session_id: str = "abc12345"
    project_path: str = "/test"
    project_slug: str = "test-project"
    start_time: str = "2026-04-01T00:00:00Z"
    end_time: str = "2026-04-01T01:00:00Z"
    git_branch: str = "main"
    user_prompts: list = field(default_factory=lambda: [FakePrompt()])
    total_turns: int = 5


DEFAULT_CONFIG = {
    "skip_projects": [],
}


class TestShouldSkip:
    def test_normal_session_not_skipped(self):
        with patch("chronicle.filtering.already_chronicled", return_value=False):
            assert should_skip(FakeDigest(), DEFAULT_CONFIG) is None

    def test_zero_turns_not_skipped(self):
        """Every session gets recorded, even with zero turns."""
        digest = FakeDigest(total_turns=0)
        with patch("chronicle.filtering.already_chronicled", return_value=False):
            assert should_skip(digest, DEFAULT_CONFIG) is None

    def test_no_user_prompts_not_skipped(self):
        """Sessions with no prompts still get recorded."""
        digest = FakeDigest(user_prompts=[])
        with patch("chronicle.filtering.already_chronicled", return_value=False):
            assert should_skip(digest, DEFAULT_CONFIG) is None

    def test_self_session_detected(self):
        prompt = FakePrompt(text="You are a Decision Chronicler reviewing a session")
        digest = FakeDigest(user_prompts=[prompt])
        reason = should_skip(digest, DEFAULT_CONFIG)
        assert reason == "chronicle self-session"

    def test_skip_projects(self):
        config = {"skip_projects": ["test-project"]}
        reason = should_skip(FakeDigest(), config)
        assert reason == "project in skip list"

    def test_already_chronicled(self):
        with patch("chronicle.filtering.already_chronicled", return_value=True):
            reason = should_skip(FakeDigest(), DEFAULT_CONFIG)
            assert reason == "already chronicled"

    def test_force_skips_already_check(self):
        with patch("chronicle.filtering.already_chronicled", return_value=True):
            reason = should_skip(FakeDigest(), DEFAULT_CONFIG, force=True)
            assert reason is None
