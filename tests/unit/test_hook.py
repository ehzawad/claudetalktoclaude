"""Unit tests for chronicle.hook.

The hook is the product-safety boundary — if this behaves wrong, users
either get surprise token burn (spawning daemon in foreground) or lose
past-session context injection. Covers:

- All four event types (SessionStart / UserPromptSubmit / Stop / SessionEnd)
  append a single line to events.jsonl, with chronicle_timestamp stamped.
- SessionStart in background mode + no running daemon → spawns daemon.
- SessionStart in foreground mode → NEVER spawns daemon.
- SessionStart with past session titles → emits additionalContext JSON.
- SessionStart without past titles → no additionalContext emitted.
- Non-SessionStart events never spawn daemon, regardless of mode.
- Errors during hook execution are trapped (never block the session).
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


def _call_hook(event: dict, monkeypatch, capsys) -> tuple[str, str]:
    """Feed `event` to chronicle.hook.main() via stdin. Return (stdout, stderr)."""
    import chronicle.hook as hook_mod
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    hook_mod.main()
    cap = capsys.readouterr()
    return cap.out, cap.err


def _read_events_jsonl(chronicle_dir: Path) -> list[dict]:
    p = chronicle_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


class TestEventLogging:
    def test_userpromptsubmit_appends_event(self, chronicle_env, monkeypatch, capsys):
        _call_hook({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sid-1",
            "prompt": "hello",
            "cwd": "/tmp/x",
        }, monkeypatch, capsys)
        events = _read_events_jsonl(chronicle_env["chronicle_dir"])
        assert len(events) == 1
        assert events[0]["hook_event_name"] == "UserPromptSubmit"
        assert events[0]["session_id"] == "sid-1"
        assert "chronicle_timestamp" in events[0]

    def test_stop_appends_event(self, chronicle_env, monkeypatch, capsys):
        _call_hook({
            "hook_event_name": "Stop",
            "session_id": "sid-2",
            "transcript_path": "/tmp/fake.jsonl",
        }, monkeypatch, capsys)
        events = _read_events_jsonl(chronicle_env["chronicle_dir"])
        assert len(events) == 1
        assert events[0]["hook_event_name"] == "Stop"

    def test_sessionend_appends_event(self, chronicle_env, monkeypatch, capsys):
        _call_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "sid-3",
            "reason": "prompt_input_exit",
        }, monkeypatch, capsys)
        events = _read_events_jsonl(chronicle_env["chronicle_dir"])
        assert len(events) == 1
        assert events[0]["hook_event_name"] == "SessionEnd"


class TestDaemonSpawnGating:
    def test_foreground_never_spawns(self, chronicle_env, monkeypatch, capsys):
        """Default foreground mode MUST NOT spawn the daemon on SessionStart."""
        import chronicle.hook as hook_mod
        spawn_calls = []
        monkeypatch.setattr(hook_mod, "_spawn_daemon",
                            lambda: spawn_calls.append("spawned"))
        monkeypatch.setattr(hook_mod, "_daemon_running", lambda: False)
        _call_hook({
            "hook_event_name": "SessionStart",
            "session_id": "sid-fg",
            "cwd": "/tmp/x",
        }, monkeypatch, capsys)
        assert spawn_calls == [], (
            "foreground mode must not spawn daemon — token-safety bug"
        )

    def test_background_spawns_when_dead(self, chronicle_env, monkeypatch, capsys):
        from chronicle import mode
        import chronicle.hook as hook_mod
        mode.set_processing_mode("background")
        # Reload hook so its `is_background_mode` closure picks up new config
        import importlib
        importlib.reload(hook_mod)

        spawn_calls = []
        monkeypatch.setattr(hook_mod, "_spawn_daemon",
                            lambda: spawn_calls.append("spawned"))
        monkeypatch.setattr(hook_mod, "_daemon_running", lambda: False)
        _call_hook({
            "hook_event_name": "SessionStart",
            "session_id": "sid-bg",
            "cwd": "/tmp/x",
        }, monkeypatch, capsys)
        assert spawn_calls == ["spawned"]

    def test_background_does_not_spawn_when_alive(
        self, chronicle_env, monkeypatch, capsys,
    ):
        from chronicle import mode
        import chronicle.hook as hook_mod
        mode.set_processing_mode("background")
        import importlib
        importlib.reload(hook_mod)

        spawn_calls = []
        monkeypatch.setattr(hook_mod, "_spawn_daemon",
                            lambda: spawn_calls.append("spawned"))
        monkeypatch.setattr(hook_mod, "_daemon_running", lambda: True)
        _call_hook({
            "hook_event_name": "SessionStart",
            "session_id": "sid-bg-alive",
            "cwd": "/tmp/x",
        }, monkeypatch, capsys)
        assert spawn_calls == [], (
            "daemon already running; hook should not double-spawn"
        )

    def test_non_session_start_never_spawns(self, chronicle_env, monkeypatch, capsys):
        """Only SessionStart triggers spawn logic, even in background mode."""
        from chronicle import mode
        import chronicle.hook as hook_mod
        mode.set_processing_mode("background")
        import importlib
        importlib.reload(hook_mod)

        spawn_calls = []
        monkeypatch.setattr(hook_mod, "_spawn_daemon",
                            lambda: spawn_calls.append("spawned"))
        monkeypatch.setattr(hook_mod, "_daemon_running", lambda: False)
        for event in ("UserPromptSubmit", "Stop", "SessionEnd"):
            _call_hook({
                "hook_event_name": event, "session_id": f"sid-{event}",
                "cwd": "/tmp/x",
            }, monkeypatch, capsys)
        assert spawn_calls == [], (
            f"non-SessionStart events should not spawn daemon; got {spawn_calls}"
        )


class TestAdditionalContextInjection:
    def _seed_session_md(self, chronicle_dir: Path, slug: str, title: str):
        sd = chronicle_dir / "projects" / slug / "sessions"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / f"2026-04-17_0000_abc_{slug}.md").write_text(f"# {title}\n")

    def test_injects_recent_titles_when_present(
        self, chronicle_env, monkeypatch, capsys,
    ):
        slug = "-tmp-x"
        self._seed_session_md(chronicle_env["chronicle_dir"], slug,
                              "past decision: wire hooks")
        out, _ = _call_hook({
            "hook_event_name": "SessionStart",
            "session_id": "sid-s",
            "cwd": "/tmp/x",
        }, monkeypatch, capsys)
        assert out.strip(), "expected hookSpecificOutput JSON on stdout"
        payload = json.loads(out)
        assert "hookSpecificOutput" in payload
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "past decision: wire hooks" in ctx
        assert "Previous sessions" in ctx

    def test_no_output_when_no_titles(self, chronicle_env, monkeypatch, capsys):
        """No past sessions → hook should emit nothing to stdout (no empty JSON)."""
        out, _ = _call_hook({
            "hook_event_name": "SessionStart",
            "session_id": "sid-s",
            "cwd": "/tmp/does-not-exist",
        }, monkeypatch, capsys)
        # Stdout must be empty (otherwise Claude Code sees an empty context line)
        assert out.strip() == ""


class TestSpawnDaemonCommand:
    """`_spawn_daemon_cmd` has to branch on sys.frozen so the respawn works
    under both the PyInstaller binary and dev checkouts. Regression against
    the silent-no-op bug where `[sys.executable, '-m', 'chronicle.daemon']`
    was fed to the frozen binary (which ignores `-m`)."""

    def test_dev_uses_module_entry(self, monkeypatch):
        import chronicle.hook as hook_mod
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        cmd = hook_mod._spawn_daemon_cmd()
        assert cmd == [sys.executable, "-m", "chronicle.daemon"]

    def test_frozen_uses_subcommand(self, monkeypatch):
        import chronicle.hook as hook_mod
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        cmd = hook_mod._spawn_daemon_cmd()
        assert cmd == [sys.executable, "daemon"], (
            "PyInstaller bootloader ignores -m; must use 'daemon' subcommand"
        )


class TestErrorTrapping:
    def test_malformed_stdin_does_not_crash(self, chronicle_env, monkeypatch):
        """A bug in hook.py MUST NOT propagate — it would block the user's
        Claude Code session. Errors are logged to ~/.chronicle/hook-errors.log."""
        import chronicle.hook as hook_mod
        monkeypatch.setattr(sys, "stdin", io.StringIO("not valid json"))
        # Should not raise
        hook_mod.main()
        err_log = chronicle_env["chronicle_dir"] / "hook-errors.log"
        assert err_log.exists(), "hook-errors.log should capture the traceback"
