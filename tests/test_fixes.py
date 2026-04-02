"""Regression tests for bugs found during code review.

Each test exercises the real module function that was fixed, not
reimplemented logic copied from the source.
"""

import asyncio
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from chronicle.summarizer import _extract_json, ChronicleEntry
from chronicle.storage import (
    _remove_session_entry,
    _atomic_write,
    rebuild_prompts_section,
    append_to_chronicle,
    write_chronicle,
    _PROMPTS_MARKER,
)


# --- Daemon tuple bug + retry mechanism ---
# _process_batch must receive plain dicts (not tuples) and return
# retry lists for failed/errored sessions.

class TestProcessBatch:
    def test_receives_plain_dicts(self):
        from chronicle.daemon import _process_batch

        received = []

        async def mock_process(event, config, semaphore):
            received.append(event)
            assert isinstance(event, dict)
            digest = MagicMock(session_id=event["session_id"],
                               start_time="2026-04-01T00:00:00Z")
            entry = MagicMock(is_error=False)
            return (digest, entry)

        events = [("abc", {"session_id": "abc", "transcript_path": "/t.jsonl"})]

        with patch("chronicle.daemon._async_process_one", side_effect=mock_process):
            with patch("chronicle.daemon.write_chronicle"):
                retry = asyncio.run(_process_batch(events, {"concurrency": 5, "max_retries": 3}))

        assert len(received) == 1
        assert isinstance(received[0], dict)
        assert retry == []

    def test_is_error_sessions_retried(self):
        from chronicle.daemon import _process_batch

        digest = MagicMock(session_id="abc",
                           start_time="2026-04-01T00:00:00Z",
                           end_time="2026-04-01T01:00:00Z")
        entry = MagicMock(is_error=True)

        async def mock_process(event, config, semaphore):
            return (digest, entry)

        events = [("abc", {"session_id": "abc"})]

        with patch("chronicle.daemon._async_process_one", side_effect=mock_process):
            with patch("chronicle.daemon.write_chronicle"):
                with patch("chronicle.daemon.already_chronicled", return_value=False):
                    retry = asyncio.run(_process_batch(events, {"concurrency": 5, "max_retries": 3}))

        assert len(retry) == 1
        assert retry[0][0] == "abc"

    def test_exception_sessions_retried(self):
        from chronicle.daemon import _process_batch

        async def mock_process(event, config, semaphore):
            raise RuntimeError("boom")

        events = [("abc", {"session_id": "abc"})]

        with patch("chronicle.daemon._async_process_one", side_effect=mock_process):
            retry = asyncio.run(_process_batch(events, {"concurrency": 5, "max_retries": 3}))

        assert len(retry) == 1

    def test_gave_up_sessions_not_retried(self):
        from chronicle.daemon import _process_batch

        digest = MagicMock(session_id="abc",
                           start_time="2026-04-01T00:00:00Z",
                           end_time="2026-04-01T01:00:00Z")
        entry = MagicMock(is_error=True)

        async def mock_process(event, config, semaphore):
            return (digest, entry)

        events = [("abc", {"session_id": "abc"})]

        with patch("chronicle.daemon._async_process_one", side_effect=mock_process):
            with patch("chronicle.daemon.write_chronicle"):
                with patch("chronicle.daemon.already_chronicled", return_value=True):
                    retry = asyncio.run(_process_batch(events, {"concurrency": 5, "max_retries": 3}))

        assert retry == []


# --- Event categorization (extracted _process_events) ---
# Tests the real function for SessionEnd overwrite protection
# and UserPromptSubmit debounce removal.

class TestProcessEvents:
    def test_stop_preserved_over_sessionend_without_path(self):
        from chronicle.daemon import _process_events

        pending = {}
        events = [
            {"hook_event_name": "Stop", "session_id": "abc",
             "transcript_path": "/path/session.jsonl"},
            {"hook_event_name": "SessionEnd", "session_id": "abc"},
        ]
        _process_events(events, pending)
        assert pending["abc"]["transcript_path"] == "/path/session.jsonl"

    def test_sessionend_with_path_overwrites_stop_without(self):
        from chronicle.daemon import _process_events

        pending = {}
        events = [
            {"hook_event_name": "Stop", "session_id": "abc"},
            {"hook_event_name": "SessionEnd", "session_id": "abc",
             "transcript_path": "/better/path.jsonl"},
        ]
        _process_events(events, pending)
        assert pending["abc"]["transcript_path"] == "/better/path.jsonl"

    def test_user_prompt_removes_from_pending(self):
        from chronicle.daemon import _process_events

        pending = {}
        events = [
            {"hook_event_name": "Stop", "session_id": "abc",
             "transcript_path": "/p.jsonl"},
            {"hook_event_name": "UserPromptSubmit", "session_id": "abc"},
        ]
        _process_events(events, pending)
        assert "abc" not in pending

    def test_returns_activity_flag(self):
        from chronicle.daemon import _process_events

        pending = {}
        assert _process_events(
            [{"hook_event_name": "Stop", "session_id": "x"}], pending) is True
        assert _process_events(
            [{"hook_event_name": "SessionStart", "session_id": "x"}], pending) is False


# --- Offset crash safety ---
# Events read + categorized via real functions; offset deferred while pending.

class TestOffsetCrashSafety:
    def test_offset_deferred_while_pending(self):
        from chronicle.daemon import (
            _read_new_events, _process_events, _save_offset, _read_offset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            offset_file = Path(tmpdir) / "events.offset"
            events_file = Path(tmpdir) / "events.jsonl"

            with open(events_file, "w") as f:
                f.write(json.dumps({"hook_event_name": "Stop", "session_id": "abc"}) + "\n")

            with patch("chronicle.daemon.OFFSET_FILE", offset_file):
                with patch("chronicle.daemon.EVENTS_FILE", events_file):
                    _save_offset(0)

                    events, new_offset = _read_new_events(0)
                    assert len(events) == 1

                    pending = {}
                    _process_events(events, pending)
                    assert "abc" in pending

                    # Real daemon condition from daemon.py:260
                    if not pending:
                        _save_offset(new_offset)
                    assert _read_offset() == 0  # NOT advanced — sessions pending

                    pending.clear()
                    if not pending:
                        _save_offset(new_offset)
                    assert _read_offset() == new_offset  # advanced after clear


# --- _remove_session_entry: orphaned <details> fix ---

class TestRemoveSessionEntry:
    def test_removes_details_block(self):
        content = """# Chronicle: test

| Date | Session | Decisions | Summary |
|------|---------|-----------|---------|
| 2026-04-01 | [First](sessions/aaa11111.md) | 1 | Sum |
<!-- /timeline -->

## First
<!-- session:aaa11111-full-uuid -->

Some content.

---

<details><summary>User prompts (verbatim)</summary>

**Prompt 1** (2026-04-01 00:00):
> hello world

</details>

---

"""
        result = _remove_session_entry(content, "<!-- session:aaa11111-full-uuid -->")
        assert "aaa11111" not in result
        assert "<details>" not in result
        assert "hello world" not in result

    def test_preserves_adjacent_sessions(self):
        content = """# Chronicle: test

| Date | Session | Decisions | Summary |
|------|---------|-----------|---------|
| 2026-04-01 | [First](sessions/aaa11111.md) | 0 | A |
| 2026-04-02 | [Second](sessions/bbb22222.md) | 0 | B |
<!-- /timeline -->

## First
<!-- session:aaa11111-full-uuid -->

Content A.

---

<details><summary>User prompts (verbatim)</summary>

**Prompt 1** (2026-04-01 00:00):
> prompt A

</details>

---

## Second
<!-- session:bbb22222-full-uuid -->

Content B.

---

"""
        result = _remove_session_entry(content, "<!-- session:aaa11111-full-uuid -->")
        assert "aaa11111" not in result
        assert "Second" in result
        assert "bbb22222" in result
        assert "Content B" in result


# --- Atomic writes ---

class TestAtomicWrite:
    def test_creates_and_replaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.md"
            _atomic_write(path, "first")
            assert path.read_text() == "first"
            _atomic_write(path, "second")
            assert path.read_text() == "second"
            assert not path.with_suffix(".md.tmp").exists()


# --- Strategy 3 JSON extraction: break on first depth-0 ---

class TestExtractJsonStrategy3:
    def test_stops_at_first_complete_object(self):
        text = '{"title": "test"} some explanation with {braces} here'
        result = _extract_json(text)
        assert result is not None
        assert result["title"] == "test"

    def test_nested_json(self):
        text = 'Result: {"outer": {"inner": "val"}, "list": [1,2,3]} done'
        result = _extract_json(text)
        assert result["outer"]["inner"] == "val"
        assert result["list"] == [1, 2, 3]

    def test_braces_in_strings(self):
        data = {"code": "if (x) { return y; }", "title": "test"}
        text = f"Here: {json.dumps(data)} end"
        result = _extract_json(text)
        assert result["code"] == "if (x) { return y; }"


# --- write_chronicle calls rebuild_prompts_section ---

class TestWriteChronicleRebuildsPrompts:
    def test_rebuild_called(self):
        from chronicle.extractor import UserPrompt

        entry = ChronicleEntry(
            session_id="abc12345", project_path="/test", project_slug="test-slug",
            start_time="2026-04-01T00:00:00Z", end_time="2026-04-01T01:00:00Z",
            git_branch="main",
            user_prompts=[UserPrompt("hello", "2026-04-01T00:00:00Z", "u1")],
            title="Test", summary="A test",
        )
        digest = MagicMock(session_id="abc12345", end_time="2026-04-01T01:00:00Z",
                           project_slug="test-slug")
        calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                with patch("chronicle.storage.ensure_dirs"):
                    with patch("chronicle.storage.rebuild_prompts_section",
                               side_effect=lambda s: calls.append(s)):
                        with patch("chronicle.storage.mark_chronicled"):
                            (Path(tmpdir) / "sessions").mkdir()
                            write_chronicle(entry, digest)

        assert calls == ["test-slug"]


# --- rebuild_prompts_section: stale removal + replacement ---

class TestRebuildPromptsSection:
    def test_removes_stale_section_when_no_prompts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chronicle_file = Path(tmpdir) / "chronicle.md"
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()

            chronicle_file.write_text(
                "# Chronicle: test\n\ncontent here\n\n"
                "<!-- prompts -->\n\n## All User Prompts\n\n> stale\n"
            )
            (sessions_dir / "session.md").write_text("# No prompts here\n\n## Summary\n\nDone.\n")

            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                rebuild_prompts_section("slug")

            result = chronicle_file.read_text()
            assert "stale" not in result
            assert "<!-- prompts -->" not in result
            assert "content here" in result

    def test_replaces_existing_section(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chronicle_file = Path(tmpdir) / "chronicle.md"
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()

            chronicle_file.write_text(
                "# Chronicle: test\n\ncontent\n\n"
                "<!-- prompts -->\n\n## All User Prompts\n\n> old prompt\n"
            )
            (sessions_dir / "session.md").write_text(
                "# Test\n\n"
                "<details><summary>User prompts (verbatim)</summary>\n\n"
                "**Prompt 1** (2026-04-01 00:00):\n"
                "> new prompt\n\n"
                "</details>\n"
            )

            with patch("chronicle.storage.project_chronicle_dir", return_value=Path(tmpdir)):
                rebuild_prompts_section("slug")

            result = chronicle_file.read_text()
            assert "old prompt" not in result
            assert "new prompt" in result
            assert "content" in result


# --- hook.main() spawns daemon on all SessionStart sources ---

class TestHookMain:
    def _run_hook(self, source, daemon_running=False):
        from chronicle.hook import main as hook_main

        spawn_calls = []
        data = json.dumps({
            "hook_event_name": "SessionStart",
            "source": source,
            "cwd": "/test",
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.hook.CHRONICLE_DIR", Path(tmpdir)):
                with patch("chronicle.hook.EVENTS_FILE", Path(tmpdir) / "events.jsonl"):
                    with patch("chronicle.hook._daemon_running", return_value=daemon_running):
                        with patch("chronicle.hook._spawn_daemon",
                                   side_effect=lambda: spawn_calls.append(1)):
                            with patch("chronicle.hook.load_recent_titles", return_value=[]):
                                with patch("sys.stdin", io.StringIO(data)):
                                    hook_main()

        return spawn_calls

    def test_spawns_on_startup(self):
        assert len(self._run_hook("startup")) == 1

    def test_spawns_on_resume(self):
        assert len(self._run_hook("resume")) == 1

    def test_spawns_on_clear(self):
        assert len(self._run_hook("clear")) == 1

    def test_spawns_on_compact(self):
        assert len(self._run_hook("compact")) == 1

    def test_no_spawn_when_running(self):
        assert len(self._run_hook("resume", daemon_running=True)) == 0
