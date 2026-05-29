"""Regression tests for the Claude-audit + Codex-council fix batch.

Each test pins a specific confirmed bug so it cannot silently regress.
BUG ids refer to the audit reconciliation (see the PR description).

Covered here:
  BUG-02  project_slug_for matches Claude Code's per-character dir transform
  BUG-22  top-level --help/-h/help prints usage and exits 0
  BUG-14  processing_lock_held() never raises when the lock can't be probed
  BUG-19  mark_succeeded writes the success marker atomically (no .tmp leak)
  BUG-13  async_batch_process floors workers>=1 (Semaphore(0) no longer hangs)
  BUG-13b _process_batch floors daemon concurrency>=1 (no ValueError/TypeError)
  BUG-05  multi-paragraph verbatim prompts survive the chronicle.md rebuild
  BUG-03a secrets in user prompts / assistant prose are redacted at extraction
  BUG-24  summarizer redacts secrets in error_message before persist/print
  BUG-07  cancelling spawn_claude kills+reaps the claude -p child (no orphan)
  BUG-18  rewind --prune declines gracefully on EOF/closed stdin
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------- BUG-02 ----------------

class TestProjectSlugForBug02:
    def test_per_character_no_run_collapse(self):
        from chronicle.config import project_slug_for
        # Claude Code replaces EVERY non-alnum with '-' and does NOT collapse
        # runs, so a leading dot yields a double dash.
        assert project_slug_for("/Users/x/.config/nvim") == "-Users-x--config-nvim"
        assert project_slug_for("/tmp/a_.b") == "-tmp-a--b"

    def test_plain_path_identical_under_both_transforms(self):
        from chronicle.config import project_slug_for
        # The pre-fix fixture path stays identical (keeps legacy tests green).
        assert project_slug_for("/tmp/x") == "-tmp-x"

    def test_transcript_path_is_authoritative(self):
        from chronicle.config import project_slug_for
        slug = project_slug_for(
            "/Users/x/.config/nvim",
            transcript_path="/Users/x/.claude/projects/-Users-x--config-nvim/abc.jsonl",
        )
        assert slug == "-Users-x--config-nvim"

    def test_trailing_slash_and_root(self):
        from chronicle.config import project_slug_for
        assert project_slug_for("/tmp/proj/") == "-tmp-proj"
        # Root must not collapse to an empty slug.
        assert project_slug_for("/") != ""


# ---------------- BUG-22 ----------------

class TestHelpExitBug22:
    @pytest.mark.parametrize("flag", ["--help", "-h", "help"])
    def test_help_prints_usage_and_exits_zero(self, flag, monkeypatch, capsys):
        from chronicle import __main__ as cli
        monkeypatch.setattr(sys, "argv", ["chronicle", flag])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Unknown command" not in out
        assert out.strip()  # the usage docstring was printed


# ---------------- BUG-14 ----------------

class TestProcessingLockHeldBug14:
    def test_no_raise_when_open_fails(self, chronicle_env, monkeypatch):
        from chronicle import locks
        lp = locks.processing_lock_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("")  # exists() is True so we reach the os.open probe
        boom = lambda *a, **k: (_ for _ in ()).throw(OSError("cannot open"))
        monkeypatch.setattr(os, "open", boom)
        # A read-only diagnostic must report not-held instead of crashing.
        assert locks.processing_lock_held() is False

    def test_no_stray_lock_file_on_toctou_race(self, chronicle_env, monkeypatch):
        # PR #1 review (C3): the probe must use O_RDONLY (not O_CREAT). Simulate
        # the TOCTOU race where exists() passes but the file is gone at open():
        # it must report not-held AND must not create a stray lock file.
        from chronicle import locks
        lp = locks.processing_lock_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        assert not lp.exists()
        monkeypatch.setattr(type(lp), "exists", lambda self: True)
        assert locks.processing_lock_held() is False
        # os.path.exists is not monkeypatched -> checks the real filesystem.
        assert not os.path.exists(str(lp)), "read-only probe created a stray lock file"


# ---------------- BUG-19 ----------------

class TestAtomicMarkerBug19:
    def test_mark_succeeded_atomic_no_tmp(self, chronicle_env):
        from chronicle import storage
        storage.mark_succeeded("sess-abc-19", "2026-05-29T10:00:00Z", 0.0123)
        pd = storage.processed_dir()
        marker = pd / storage.session_hash("sess-abc-19")
        assert marker.exists()
        assert marker.read_text().startswith("sess-abc-19\n")
        # The atomic write must not leave a temp file behind on success.
        assert list(pd.glob("*.tmp")) == []


# ---------------- BUG-13 / BUG-13b ----------------

class TestSemaphoreFloor:
    async def test_daemon_concurrency_floor_no_raise(self, chronicle_env):
        from chronicle.daemon import _process_batch
        # Pre-fix: Semaphore(-1) raises ValueError and Semaphore("bad") raises
        # TypeError at construction. Post-fix: floored/defaulted to a valid int.
        assert await _process_batch([], {"concurrency": -1}) == []
        assert await _process_batch([], {"concurrency": "bad"}) == []
        assert await _process_batch([], {"concurrency": 0}) == []

    async def test_batch_workers_floor_no_hang(
        self, chronicle_env, seed_session, fake_claude_bin, monkeypatch,
    ):
        env = chronicle_env
        seed_session(env["claude_projects"], "-tmp-proj13", prompts=["hello world"])
        monkeypatch.setenv("PATH", f"{fake_claude_bin}:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_CLAUDE_MODE", "success")
        from chronicle import claude_cli, batch
        claude_cli._reset_cache_for_tests()
        # workers=0 -> pre-fix Semaphore(0) blocks the lone worker forever, so
        # this would time out. Post-fix it is floored to 1 and completes.
        await asyncio.wait_for(batch.async_batch_process(workers=0), timeout=25)


# ---------------- BUG-05 ----------------

class TestMultiParagraphPromptBug05:
    def test_internal_blank_line_survives_rebuild(self, chronicle_env):
        from chronicle import storage
        from chronicle.config import project_chronicle_dir
        from chronicle.summarizer import ChronicleEntry
        from chronicle.extractor import UserPrompt
        slug = "-tmp-proj5"
        entry = ChronicleEntry(
            session_id="sess-5-aaaaaaaa", project_path="/tmp/p", project_slug=slug,
            start_time="2026-05-29T15:00:00Z", end_time="2026-05-29T15:30:00Z",
            git_branch="main",
            user_prompts=[UserPrompt(
                text="first paragraph\n\nsecond paragraph",
                timestamp="2026-05-29T15:00:00Z", uuid="u5",
            )],
            title="S5", summary="Sum5.", narrative="N5",
            decisions=[{"what": "D", "why": "B", "status": "done"}],
            total_turns=3, total_cost_usd=0.01,
        )

        class D:
            session_id = "sess-5-aaaaaaaa"
            project_slug = slug
            end_time = "2026-05-29T15:30:00Z"

        storage.write_chronicle(entry, D(), max_retries=3)
        content = (project_chronicle_dir(slug) / "chronicle.md").read_text()
        # Inspect only the rebuilt chronological prompts block (after the marker),
        # which is what rebuild_prompts_section truncated pre-fix.
        assert "<!-- prompts -->" in content
        prompts_block = content.split("<!-- prompts -->", 1)[1]
        assert "first paragraph" in prompts_block
        assert "second paragraph" in prompts_block


# ---------------- BUG-03a ----------------

class TestProseRedactionBug03a:
    def test_user_and_assistant_prose_redacted(self, tmp_path):
        from chronicle import extractor
        jsonl = tmp_path / "s.jsonl"
        lines = [
            json.dumps({
                "type": "user", "uuid": "u1", "sessionId": "s",
                "timestamp": "2026-05-29T10:00:00Z", "cwd": "/tmp/p",
                "gitBranch": "main",
                "message": {"content": "here is my key sk-ant-api03-SECRETVALUE123 ok"},
            }),
            json.dumps({
                "type": "assistant", "uuid": "a1", "sessionId": "s",
                "timestamp": "2026-05-29T10:00:01Z",
                "message": {"content": [
                    {"type": "text", "text": "use ghp_DEADBEEF1234567890abcd to auth"},
                ]},
            }),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        digest = extractor.extract_session(str(jsonl))

        prompts = " ".join(p.text for p in digest.user_prompts)
        assert "sk-ant-api03-SECRETVALUE123" not in prompts
        assert "[REDACTED]" in prompts

        responses = " ".join(digest.assistant_responses)
        assert "ghp_DEADBEEF1234567890abcd" not in responses
        assert "[REDACTED]" in responses


# ---------------- BUG-24 ----------------

class TestErrorMessageRedactionBug24:
    async def test_error_message_is_redacted(self, tmp_path, monkeypatch):
        from chronicle import summarizer, extractor
        from chronicle.claude_cli import ErrorKind, ClaudeResult
        jsonl = tmp_path / "s.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user", "uuid": "u", "sessionId": "s",
            "timestamp": "2026-05-29T10:00:00Z", "cwd": "/tmp/p",
            "gitBranch": "main", "message": {"content": "hello"},
        }) + "\n")
        digest = extractor.extract_session(str(jsonl))

        async def fake_spawn(*a, **k):
            return ClaudeResult(
                error_kind=ErrorKind.TRANSIENT,
                error_message="failed: token=ghp_DEADBEEF1234567890abcd leaked",
            )

        monkeypatch.setattr(summarizer, "spawn_claude", fake_spawn)
        entry = await summarizer.async_summarize_session(digest)
        assert entry.is_error
        assert "ghp_DEADBEEF1234567890abcd" not in entry.error_message
        assert "[REDACTED]" in entry.error_message


# ---------------- BUG-07 ----------------

class TestSpawnClaudeCancelBug07:
    async def test_cancel_kills_child(self, tmp_path, monkeypatch):
        from chronicle import claude_cli
        pidfile = tmp_path / "child.pid"
        bindir = tmp_path / "bin"
        bindir.mkdir(parents=True)
        stub = bindir / "claude"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "sys.stdin.read()\n"
            "time.sleep(120)\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
        claude_cli._reset_cache_for_tests()

        task = asyncio.create_task(
            claude_cli.spawn_claude("p", model="opus", fallback_model="sonnet")
        )
        # Wait for the child to spawn and record its pid.
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Post-fix: the finally kills+reaps the child before unregistering.
        for _ in range(100):
            if not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not _pid_alive(child_pid), "claude -p child orphaned after cancel"
        assert claude_cli.active_subprocess_count() == 0
        claude_cli._reset_cache_for_tests()

    async def test_double_cancel_no_registry_leak(self, tmp_path, monkeypatch):
        # PR #1 review (C1): a SECOND CancelledError landing inside the finally's
        # awaited reap must not skip _unregister. The single-cancel test above
        # passes even on the buggy ordering; this re-entrant-cancel test does not.
        from chronicle import claude_cli
        pidfile = tmp_path / "child2.pid"
        bindir = tmp_path / "bin2"
        bindir.mkdir(parents=True)
        stub = bindir / "claude"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "sys.stdin.read()\n"
            "time.sleep(120)\n"
        )
        stub.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
        claude_cli._reset_cache_for_tests()

        assert claude_cli.active_subprocess_count() == 0
        task = asyncio.create_task(
            claude_cli.spawn_claude("p", model="opus", fallback_model="sonnet")
        )
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        child_pid = int(pidfile.read_text().strip())
        assert claude_cli.active_subprocess_count() == 1

        # Cancel repeatedly across event-loop turns so a second CancelledError
        # lands during the finally's awaited reap.
        for _ in range(8):
            task.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if claude_cli.active_subprocess_count() == 0 and not _pid_alive(child_pid):
                break
            await asyncio.sleep(0.02)
        assert claude_cli.active_subprocess_count() == 0, "registry leaked under re-entrant cancel"
        assert not _pid_alive(child_pid), "child orphaned under re-entrant cancel"
        claude_cli._reset_cache_for_tests()


# ---------------- BUG-18 ----------------

class TestPruneEofBug18:
    def test_prune_declines_on_eof(self, chronicle_env, monkeypatch, capsys):
        from chronicle import rewind
        env = chronicle_env
        project_dir = env["chronicle_dir"] / "projects" / "-tmp-prune"
        (project_dir / "sessions").mkdir(parents=True)
        sess_md = project_dir / "sessions" / "2026-05-29_x.md"
        sess_md.write_text("# Empty session\n")
        sessions = [{
            "n_decisions": 0, "number": 1, "date": "2026-05-29T10:00",
            "title": "Empty session", "path": str(sess_md),
        }]

        def _raise_eof(*a, **k):
            raise EOFError()

        monkeypatch.setattr("builtins.input", _raise_eof)
        # Must not raise; must decline; must NOT delete the file.
        rewind.prune_empty_sessions(sessions, project_dir)
        out = capsys.readouterr().out
        assert "Cancelled" in out
        assert sess_md.exists()
