"""Fail-before/pass-after regression tests for cross-module fixes.

Covered here:
  BUG-03b  JSON-quoted credentials redacted; benign compact JSON not over-redacted
  BUG-08   foreground events.jsonl is capped; background is not truncated
  BUG-10   daemon liveness is flock-authoritative (stale pid + unrelated live PID)
  BUG-12a  one per-session write failure does not abort the whole batch
  BUG-12b  non-dict structured field coerced -> no AttributeError in markdown
  BUG-15   bare-string list field coerced -> one bullet, not char-by-char
  BUG-17   query --project filters on the slug, not the full filesystem path
  BUG-25   files written by the hook are owner-only (0600) via the entry umask
  BUG-04   terminate_active_subprocesses actually kills a registered live child
"""
from __future__ import annotations

import io
import json
import os
import subprocess

import pytest


# ---------------- BUG-03b ----------------

class TestJsonRedactionBug03b:
    def test_json_quoted_credentials_redacted(self):
        from chronicle import extractor
        out = extractor._redact_secrets('{"access_token": "ya29.SECRETTOKEN", "n": 1}')
        assert "ya29.SECRETTOKEN" not in out and "[REDACTED]" in out
        out2 = extractor._redact_secrets('"refresh_token": "1//abcSECRET"')
        assert "1//abcSECRET" not in out2 and "[REDACTED]" in out2
        out3 = extractor._redact_secrets('"SecretAccessKey": "wJalrXUtnSECRET"')
        assert "wJalrXUtnSECRET" not in out3 and "[REDACTED]" in out3

    def test_benign_json_not_over_redacted(self):
        from chronicle import extractor
        # Unquoted/numeric value and a non-credential key must be left intact.
        assert extractor._redact_secrets('{"token":5,"next":"keep"}') == '{"token":5,"next":"keep"}'
        assert extractor._redact_secrets('{"description":"my token broke"}') == '{"description":"my token broke"}'


# ---------------- BUG-12b / BUG-15 ----------------

def _bare_entry():
    from chronicle.summarizer import ChronicleEntry
    return ChronicleEntry(
        session_id="s", project_path="/p", project_slug="-p",
        start_time="2026-05-29T10:00:00Z", end_time="2026-05-29T10:01:00Z",
        git_branch="main", user_prompts=[],
    )


class TestStructuredCoercion:
    def test_bare_string_list_field_not_char_iterated(self):  # BUG-15
        from chronicle import summarizer
        e = _bare_entry()
        summarizer._populate_entry_from_structured(
            {"decisions": "oops", "files_changed": "main.py"}, e)
        assert e.decisions == ["oops"]
        assert e.files_changed == ["main.py"]
        md = summarizer.entry_to_session_markdown(e)
        # Pre-fix the bare string was iterated char-by-char ("### o" / "### o" /
        # "### p" / "### s"), so "oops" never appeared contiguously; post-fix it
        # renders as a single item.
        assert "oops" in md

    def test_non_dict_field_does_not_crash_markdown(self):  # BUG-12b
        from chronicle import summarizer
        e = _bare_entry()
        summarizer._populate_entry_from_structured({"technical_details": "not a dict"}, e)
        assert e.technical_details == {}
        summarizer.entry_to_session_markdown(e)  # must not raise AttributeError


# ---------------- BUG-17 ----------------

class TestQueryProjectFilterBug17:
    def test_search_filters_by_slug_not_path(self, chronicle_env, capsys):
        from chronicle import query
        from chronicle.config import projects_dir
        for slug in ("-Users-alice-myapp", "-Users-bob-webapp"):
            (projects_dir() / slug / "sessions").mkdir(parents=True)
            (projects_dir() / slug / "chronicle.md").write_text("# C\nauthentication flow\n")
        # "projects" is in every full path but is not a slug -> zero matches.
        query.search("authentication", project="projects")
        assert "No results" in capsys.readouterr().out
        # A real slug substring still matches only the intended project.
        query.search("authentication", project="alice")
        out = capsys.readouterr().out
        assert "-Users-alice-myapp" in out and "-Users-bob-webapp" not in out


# ---------------- BUG-08 ----------------

class TestEventsCapBug08:
    def test_foreground_events_jsonl_capped(self, chronicle_env, monkeypatch):
        from chronicle import hook, mode
        from chronicle.config import events_file
        mode.set_processing_mode("foreground")
        monkeypatch.setattr(hook, "_MAX_EVENTS_BYTES", 1000)
        ef = events_file()
        ef.write_text("x" * 5000 + "\n")
        assert ef.stat().st_size > 1000
        hook._cap_events_foreground()
        assert ef.stat().st_size == 0  # whole-file truncate when over cap

    def test_background_events_not_truncated(self, chronicle_env, monkeypatch):
        from chronicle import hook, mode
        from chronicle.config import events_file
        mode.set_processing_mode("background")
        monkeypatch.setattr(hook, "_MAX_EVENTS_BYTES", 1000)
        ef = events_file()
        ef.write_text("x" * 5000 + "\n")
        hook._cap_events_foreground()
        assert ef.stat().st_size > 1000  # background is left to the daemon


# ---------------- BUG-25 ----------------

class TestPermsBug25:
    def test_hook_events_file_owner_only(self, chronicle_env, monkeypatch):
        from chronicle import hook
        from chronicle.config import events_file
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": "/tmp/x", "prompt": "hi"})
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        prev = os.umask(0o022)  # permissive default; main() must override to 0o077
        try:
            hook.main()
        finally:
            os.umask(prev)
        mode = oct(events_file().stat().st_mode & 0o777)
        assert mode == "0o600", f"events.jsonl is {mode}, expected 0o600"


# ---------------- BUG-10 ----------------

class TestDaemonLivenessBug10:
    def test_stale_pid_unrelated_process_not_running(self, chronicle_env):
        from chronicle import locks
        proc = subprocess.Popen(["sleep", "30"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            locks.pid_file().write_text(f"{proc.pid}\n")  # stale pid, no flock held
            running, pid = locks.daemon_is_running()
            assert running is False  # flock-authoritative, not os.kill on a recycled pid
        finally:
            proc.terminate()
            proc.wait()


# ---------------- BUG-12a ----------------

class TestBatchWriteGuardBug12a:
    async def test_one_write_failure_does_not_abort_batch(self, chronicle_env, monkeypatch):
        from chronicle import daemon

        class _D:
            def __init__(self, sid):
                self.session_id = sid
                self.start_time = sid

        class _E:
            is_error = False

        async def fake_one(event, config, semaphore):
            return (_D(event["sid"]), _E())

        attempted = []

        def fake_write(entry, digest, max_retries=3):
            attempted.append(digest.session_id)
            if digest.session_id == "s1":
                raise OSError("ENOSPC: no space left on device")

        monkeypatch.setattr(daemon, "_async_process_one", fake_one)
        monkeypatch.setattr(daemon, "write_chronicle", fake_write)
        events = [("s1", {"sid": "s1"}), ("s2", {"sid": "s2"})]
        # Must not raise; both sessions attempted (first failure doesn't abort).
        await daemon._process_batch(events, {"concurrency": 2, "max_retries": 3})
        assert "s1" in attempted and "s2" in attempted


# ---------------- BUG-04 ----------------

class TestTerminateActiveBug04:
    async def test_terminate_kills_registered_child(self):
        import asyncio
        from chronicle import claude_cli
        proc = await asyncio.create_subprocess_exec(
            "sleep", "30",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        claude_cli._register(proc)
        try:
            assert claude_cli.active_subprocess_count() == 1
            res = await claude_cli.terminate_active_subprocesses(grace_seconds=1.0)
            assert res.get("terminated", 0) >= 1
            # terminate kills+reaps the child; the daemon's BUG-04 race wrapper
            # calls this mid-batch so a SIGTERM actually reaches in-flight children.
            assert proc.returncode is not None
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            claude_cli._unregister(proc)
