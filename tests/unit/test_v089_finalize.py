"""Regression tests for the v0.8.9 finalize fixes (council-flagged):

  BUG-20 #4   _session_id_from_jsonl tolerates corrupt/binary jsonl (no UnicodeDecodeError)
  hook liveness   hook._daemon_running uses the flock-authoritative probe (BUG-10), not os.kill
  BUG-08 race #1  a transcript change DURING write_chronicle undoes the success marker + requeues
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest


def test_session_id_from_jsonl_unicode_safe(tmp_path):
    from chronicle.extractor import _session_id_from_jsonl
    p = tmp_path / "binary.jsonl"
    p.write_bytes(b'\xff\xfe\x00 not utf-8 garbage \x80\n{"sessionId":"real-id"}\n')
    sid = _session_id_from_jsonl(p)  # must NOT raise UnicodeDecodeError
    assert isinstance(sid, str) and sid  # falls back to stem or finds the id


def test_hook_daemon_running_is_flock_authoritative(chronicle_env):
    from chronicle import hook, locks
    proc = subprocess.Popen(["sleep", "30"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Stale pid file pointing at an unrelated live PID, no flock held.
        locks.pid_file().write_text(f"{proc.pid}\n")
        # Pre-fix (os.kill probe) returned True for the recycled PID; the
        # flock-authoritative check returns False (no daemon holds the lock).
        assert hook._daemon_running() is False
    finally:
        proc.terminate()
        proc.wait()


class TestBug08Race1Undo:
    async def test_transcript_change_during_write_undoes_marker(self, chronicle_env, monkeypatch):
        from chronicle import daemon, storage
        env = chronicle_env
        tp = env["claude_projects"] / "-tmp-r1" / "sid-r1.jsonl"
        tp.parent.mkdir(parents=True)
        tp.write_text('{"type":"user","sessionId":"sid-r1","message":{"content":"hi"}}\n')
        old = time.time() - 3600
        os.utime(tp, (old, old))  # quiet -> passes the pre-write freshness check
        before_fp = daemon._transcript_fingerprint(str(tp))

        class _D:
            session_id = "sid-r1"
            start_time = "2026-05-29T10:00:00Z"

        class _E:
            is_error = False

        async def fake_one(event, config, semaphore):
            return (_D(), _E(), before_fp)

        def fake_write(entry, digest, max_retries=3):
            # write_chronicle finalizes the session...
            storage.mark_succeeded(digest.session_id, "2026-05-29T10:00:00Z", 0.0)
            # ...but the transcript changes DURING the write (new user turn).
            tp.write_text(tp.read_text() + '{"type":"user","sessionId":"sid-r1"}\n')

        monkeypatch.setattr(daemon, "_async_process_one", fake_one)
        monkeypatch.setattr(daemon, "write_chronicle", fake_write)
        ev = {"transcript_path": str(tp), "session_id": "sid-r1"}
        retry = await daemon._process_batch(
            [("sid-r1", ev)], {"concurrency": 1, "max_retries": 3, "quiet_minutes": 5})

        # The stale finalization is undone and the session is requeued.
        assert ("sid-r1", ev) in retry
        assert not storage.is_succeeded("sid-r1")
