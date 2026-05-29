"""Regression coverage for the chronicle v0.8.9 reconciliation batch."""
from __future__ import annotations

import configparser
import json
import os
import plistlib
import stat
import time
from pathlib import Path


def _write_jsonl(path: Path, *, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "user",
        "uuid": "u1",
        "sessionId": session_id,
        "timestamp": "2026-05-29T10:00:00Z",
        "cwd": "/tmp/p",
        "gitBranch": "main",
        "message": {"content": "hello"},
    }) + "\n")


class TestServiceStopTimeoutsBug04:
    def test_launchd_plist_sets_exit_timeout(self, monkeypatch):
        from chronicle import service

        monkeypatch.setattr(service, "_chronicle_binary", lambda: "/usr/local/bin/chronicle")
        monkeypatch.setattr(service, "try_resolve_claude_binary", lambda: None)

        parsed = plistlib.loads(service._mac_plist_contents().encode())
        assert parsed["ExitTimeOut"] == 20

    def test_systemd_unit_sets_stop_bounds(self, monkeypatch):
        from chronicle import service

        monkeypatch.setattr(service, "_chronicle_binary", lambda: "/usr/local/bin/chronicle")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(service._linux_unit_contents())

        assert parser["Service"]["TimeoutStopSec"] == "20s"
        assert parser["Service"]["KillMode"] == "control-group"


class TestBackgroundEventsCompactionBug08:
    def test_background_compacts_only_consumed_empty_pending_file(
        self, chronicle_env, monkeypatch,
    ):
        from chronicle import daemon, mode
        from chronicle.config import events_file, offset_file

        mode.set_processing_mode("background")
        monkeypatch.setattr(daemon, "_MAX_EVENTS_BYTES", 20)
        ef = events_file()
        ef.write_bytes(b'{"a":1}\n{"b":2}\n{"c":3}\n')
        ef.chmod(0o644)
        offset = ef.stat().st_size

        new_offset = daemon._compact_events_if_safe(offset, {})

        assert new_offset == 0
        assert ef.exists()
        assert ef.stat().st_size == 0
        assert stat.S_IMODE(ef.stat().st_mode) == 0o600
        assert not offset_file().exists()

    def test_background_compaction_preserves_pending_and_unread(
        self, chronicle_env, monkeypatch,
    ):
        from chronicle import daemon, mode
        from chronicle.config import events_file

        mode.set_processing_mode("background")
        monkeypatch.setattr(daemon, "_MAX_EVENTS_BYTES", 20)
        ef = events_file()
        ef.write_bytes(b"x" * 100)

        assert daemon._compact_events_if_safe(ef.stat().st_size, {"sid": {}}) == ef.stat().st_size
        assert ef.stat().st_size == 100
        assert daemon._compact_events_if_safe(50, {}) == 50
        assert ef.stat().st_size == 100

    async def test_fresh_transcript_defers_and_requeues(
        self, chronicle_env, seed_session, monkeypatch,
    ):
        from chronicle import daemon

        jsonl = seed_session(
            chronicle_env["claude_projects"], "-tmp-v089-fresh", session_id="sid-fresh")
        os.utime(jsonl, None)

        called = False

        async def fake_summarize(_digest):
            nonlocal called
            called = True
            raise AssertionError("fresh transcript should not be summarized")

        monkeypatch.setattr(daemon, "async_summarize_session", fake_summarize)
        retry = await daemon._process_batch([
            ("sid-fresh", {"session_id": "sid-fresh", "transcript_path": str(jsonl)})
        ], {"concurrency": 1, "quiet_minutes": 5, "max_retries": 3})

        assert retry and retry[0][0] == "sid-fresh"
        assert called is False

    async def test_transcript_change_during_summary_defers_before_success_marker(
        self, chronicle_env, seed_session, monkeypatch,
    ):
        from chronicle import daemon, storage

        jsonl = seed_session(
            chronicle_env["claude_projects"], "-tmp-v089-changing", session_id="sid-changing")
        old = time.time() - 3600
        os.utime(jsonl, (old, old))

        class Entry:
            is_error = False

        async def fake_summarize(_digest):
            with open(jsonl, "a") as f:
                f.write(json.dumps({
                    "type": "assistant",
                    "uuid": "a2",
                    "sessionId": "sid-changing",
                    "timestamp": "2026-05-29T10:01:00Z",
                    "message": {"content": [{"type": "text", "text": "new"}]},
                }) + "\n")
            return Entry()

        def fail_write(*_args, **_kwargs):
            raise AssertionError("changed transcript must not be marked succeeded")

        monkeypatch.setattr(daemon, "async_summarize_session", fake_summarize)
        monkeypatch.setattr(daemon, "write_chronicle", fail_write)

        retry = await daemon._process_batch([
            ("sid-changing", {"session_id": "sid-changing", "transcript_path": str(jsonl)})
        ], {"concurrency": 1, "quiet_minutes": 5, "max_retries": 3})

        assert retry and retry[0][0] == "sid-changing"
        assert not storage.is_succeeded("sid-changing")


class TestScannerIdentityBug20:
    def test_helper_uses_last_non_meta_session_id(self, tmp_path):
        from chronicle.extractor import _session_id_from_jsonl

        jsonl = tmp_path / "stem-id.jsonl"
        jsonl.write_text("\n".join([
            json.dumps({"type": "user", "sessionId": "first"}),
            json.dumps({"type": "assistant", "sessionId": "second", "isMeta": True}),
            json.dumps({"type": "assistant", "sessionId": "last"}),
        ]) + "\n")

        assert _session_id_from_jsonl(jsonl) == "last"

    def test_scanner_keys_markers_on_internal_session_id(
        self, chronicle_env, monkeypatch,
    ):
        from chronicle import daemon, storage

        project = chronicle_env["claude_projects"] / "-tmp-v089-scan"
        jsonl = project / "filename-stem.jsonl"
        _write_jsonl(jsonl, session_id="internal-id")
        old = time.time() - 3600
        os.utime(jsonl, (old, old))

        storage.mark_succeeded("internal-id", "2026-05-29T10:00:00Z", 0.0)
        pending = {}
        assert daemon._scan_for_unprocessed(pending, {"quiet_minutes": 5}) == 0
        assert pending == {}

        storage.clear_session_markers("internal-id")
        assert daemon._scan_for_unprocessed(pending, {"quiet_minutes": 5}) == 1
        assert list(pending) == ["internal-id"]
        assert pending["internal-id"]["session_id"] == "internal-id"


class TestPermsAutoHeal:
    def test_hook_chmods_preexisting_events_file(self, chronicle_env, monkeypatch, capsys):
        import io
        import sys
        from chronicle import hook
        from chronicle.config import events_file

        ef = events_file()
        ef.write_text("")
        ef.chmod(0o644)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sid-perms",
            "cwd": "/tmp/x",
        })))

        hook.main()
        capsys.readouterr()

        assert stat.S_IMODE(ef.stat().st_mode) == 0o600

    def test_hook_chmods_preexisting_error_log(self, chronicle_env, monkeypatch):
        import io
        import sys
        from chronicle import hook

        err_log = chronicle_env["chronicle_dir"] / "hook-errors.log"
        err_log.write_text("")
        err_log.chmod(0o644)
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))

        hook.main()

        assert stat.S_IMODE(err_log.stat().st_mode) == 0o600


class TestStorageRemoveSessionEntry:
    def test_marker_on_first_line_does_not_search_from_eof(self):
        from chronicle.storage import _remove_session_entry

        marker = "<!-- session:sess-first -->"
        other = "<!-- session:sess-other -->"
        content = (
            f"{marker}\n"
            "body\n"
            "---\n"
            "## Other\n"
            f"{other}\n"
            "other body\n"
            "---\n"
        )

        out = _remove_session_entry(content, marker)

        assert marker not in out
        assert other in out
        assert "other body" in out


class TestBatchWriteMarkerGuard:
    async def test_marker_write_failure_requeues_without_aborting(
        self, chronicle_env, monkeypatch,
    ):
        from chronicle import daemon

        class Digest:
            def __init__(self, sid):
                self.session_id = sid
                self.start_time = sid

        class Entry:
            is_error = False

        async def fake_one(event, _config, _semaphore):
            return (Digest(event["sid"]), Entry())

        attempted = []

        def fake_write(_entry, digest, max_retries=3):
            attempted.append(digest.session_id)
            if digest.session_id == "s1":
                raise OSError("ENOSPC")

        def fail_record(*_args, **_kwargs):
            raise OSError("ENOSPC writing marker")

        monkeypatch.setattr(daemon, "_async_process_one", fake_one)
        monkeypatch.setattr(daemon, "write_chronicle", fake_write)
        monkeypatch.setattr(daemon, "record_failed_attempt", fail_record)

        retry = await daemon._process_batch(
            [("s1", {"sid": "s1"}), ("s2", {"sid": "s2"})],
            {"concurrency": 2, "max_retries": 3},
        )

        assert attempted == ["s1", "s2"]
        assert retry == [("s1", {"sid": "s1"})]
