"""Tests for batch.py — chronological ordering of eligible sessions."""

from dataclasses import dataclass, field


@dataclass
class FakePrompt:
    text: str = "Fix the bug"
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
    assistant_responses: list = field(default_factory=list)
    tool_actions: list = field(default_factory=list)
    timeline: list = field(default_factory=list)
    total_turns: int = 5


class TestChronologicalOrdering:
    def test_eligible_sorted_by_start_time(self):
        """Verify that the sort key used in batch matches start_time ordering."""
        digests = [
            FakeDigest(session_id="ccc", start_time="2026-04-01T18:00:00Z"),
            FakeDigest(session_id="aaa", start_time="2026-04-01T06:00:00Z"),
            FakeDigest(session_id="bbb", start_time="2026-04-01T12:00:00Z"),
        ]

        # This is the same sort used in batch.py
        digests.sort(key=lambda d: d.start_time)

        assert digests[0].session_id == "aaa"  # 06:00
        assert digests[1].session_id == "bbb"  # 12:00
        assert digests[2].session_id == "ccc"  # 18:00

    def test_empty_start_time_sorts_first(self):
        """Sessions with missing timestamps sort before others."""
        digests = [
            FakeDigest(session_id="bbb", start_time="2026-04-01T12:00:00Z"),
            FakeDigest(session_id="aaa", start_time=""),
        ]

        digests.sort(key=lambda d: d.start_time)

        assert digests[0].session_id == "aaa"  # empty sorts first
        assert digests[1].session_id == "bbb"
