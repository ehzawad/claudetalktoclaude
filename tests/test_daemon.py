"""Tests for daemon.py — offset resilience, chronological write ordering."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from chronicle.daemon import _read_offset


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
