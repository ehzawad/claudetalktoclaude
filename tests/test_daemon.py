"""Tests for daemon.py — offset resilience, chronological write ordering."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from chronicle.daemon import _read_offset, _read_new_events


class TestReadOffset:
    def test_missing_file_returns_zero(self):
        with patch("chronicle.daemon.OFFSET_FILE", Path("/nonexistent/offset")):
            assert _read_offset() == 0

    def test_valid_offset(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".offset", delete=False) as f:
            f.write("12345")
            f.flush()
            with patch("chronicle.daemon.OFFSET_FILE", Path(f.name)):
                assert _read_offset() == 12345

    def test_empty_file_returns_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".offset", delete=False) as f:
            f.write("")
            f.flush()
            with patch("chronicle.daemon.OFFSET_FILE", Path(f.name)):
                assert _read_offset() == 0

    def test_corrupt_file_returns_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".offset", delete=False) as f:
            f.write("not-a-number\n")
            f.flush()
            with patch("chronicle.daemon.OFFSET_FILE", Path(f.name)):
                assert _read_offset() == 0


class TestReadNewEvents:
    def test_offset_beyond_file_resets_to_zero(self):
        """When offset > file size (e.g. file was recreated), auto-reset to 0."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            event = {"hook_event_name": "Stop", "session_id": "abc123"}
            f.write(json.dumps(event) + "\n")
            f.flush()
            with patch("chronicle.daemon.EVENTS_FILE", Path(f.name)):
                events, new_offset = _read_new_events(999999)
                assert len(events) == 1
                assert events[0]["session_id"] == "abc123"
                assert new_offset > 0  # should be at end of actual file

    def test_normal_offset_reads_from_position(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            e1 = {"hook_event_name": "Stop", "session_id": "first"}
            e2 = {"hook_event_name": "Stop", "session_id": "second"}
            f.write(json.dumps(e1) + "\n")
            first_end = f.tell()
            f.write(json.dumps(e2) + "\n")
            f.flush()
            with patch("chronicle.daemon.EVENTS_FILE", Path(f.name)):
                # Read from after first event
                events, _ = _read_new_events(first_end)
                assert len(events) == 1
                assert events[0]["session_id"] == "second"

    def test_missing_file_returns_empty(self):
        with patch("chronicle.daemon.EVENTS_FILE", Path("/nonexistent/events.jsonl")):
            events, offset = _read_new_events(0)
            assert events == []
            assert offset == 0


class TestProcessBatchOrdering:
    """Verify _process_batch writes chronicles in chronological order."""

    def test_writes_in_chronological_order(self):
        from chronicle.daemon import _process_batch

        call_order = []

        def mock_write(entry, digest, max_retries=3):
            call_order.append(digest.start_time)

        fake_digest_a = MagicMock()
        fake_digest_a.session_id = "aaaa1111"
        fake_digest_a.start_time = "2026-04-01T12:00:00Z"
        fake_digest_a.total_turns = 5
        fake_digest_a.user_prompts = [MagicMock()]

        fake_digest_b = MagicMock()
        fake_digest_b.session_id = "bbbb2222"
        fake_digest_b.start_time = "2026-04-01T06:00:00Z"
        fake_digest_b.total_turns = 3
        fake_digest_b.user_prompts = [MagicMock()]

        fake_entry_a = MagicMock()
        fake_entry_a.is_error = False
        fake_entry_a.is_empty = False

        fake_entry_b = MagicMock()
        fake_entry_b.is_error = False
        fake_entry_b.is_empty = False

        async def mock_process(event, config, semaphore):
            sid = event.get("session_id", "")
            if sid == "aaaa1111":
                return (fake_digest_a, fake_entry_a)
            else:
                return (fake_digest_b, fake_entry_b)

        events = [
            ("aaaa1111", {"session_id": "aaaa1111"}),  # later session listed first
            ("bbbb2222", {"session_id": "bbbb2222"}),  # earlier session listed second
        ]

        with patch("chronicle.daemon._async_process_one", side_effect=mock_process):
            with patch("chronicle.daemon.write_chronicle", side_effect=mock_write):
                asyncio.run(_process_batch(events, {"concurrency": 5, "max_retries": 3}))

        # B (06:00) should be written before A (12:00)
        assert call_order == [
            "2026-04-01T06:00:00Z",
            "2026-04-01T12:00:00Z",
        ]
