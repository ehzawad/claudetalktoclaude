"""Regression tests for the no-limits + forward-compat changes.

Covers:
- claude error classification: structured-output / context failures are
  terminal kinds; everything else is retriable transient.
- digest_to_text imposes NO size cap by default (whole session summarized).
- spawn_claude refuses prompts over claude -p's 10 MiB stdin cap with a
  terminal CONTEXT error instead of spawning a doomed (and, with unlimited
  retries, infinitely repeated) call.
- the extractor captures unknown top-level message types, unknown content
  blocks, and brand-new tool names instead of silently dropping them.
"""
from __future__ import annotations

import json

from chronicle import claude_cli, extractor
from chronicle.claude_cli import ErrorKind, _classify_claude_error


class TestErrorClassification:
    def test_structured_output_retries_is_terminal_kind(self):
        msg = '{"subtype":"error_max_structured_output_retries","is_error":true}'
        assert _classify_claude_error(msg) is ErrorKind.STRUCTURED_OUTPUT

    def test_context_window_messages_are_terminal_kind(self):
        for m in ("prompt is too long",
                  "input exceeds the context window",
                  "maximum context length reached",
                  "too many tokens for this model"):
            assert _classify_claude_error(m) is ErrorKind.CONTEXT, m

    def test_generic_errors_stay_transient(self):
        assert _classify_claude_error("overloaded_error: retry") is ErrorKind.TRANSIENT
        assert _classify_claude_error("") is ErrorKind.TRANSIENT


class TestDigestNoSizeCap:
    def _digest_with(self, n_chars: int) -> extractor.SessionDigest:
        d = extractor.SessionDigest(
            session_id="s", project_path="/p", project_slug="-p",
            start_time="t", end_time="t", git_branch="main",
        )
        d.timeline.append(extractor.TimelineEntry(
            role="user", timestamp="t", text="x" * n_chars))
        return d

    def test_no_truncation_by_default(self):
        out = extractor.digest_to_text(self._digest_with(200_000))
        assert "omitted" not in out
        assert ("x" * 200_000) in out

    def test_truncates_only_when_max_chars_given(self):
        out = extractor.digest_to_text(self._digest_with(200_000), max_chars=50_000)
        assert "omitted" in out
        assert len(out) < 100_000


class TestStdinCapGuard:
    async def test_oversized_prompt_is_terminal_context(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        claude_cli._reset_cache_for_tests()

        huge = "x" * (10 * 1024 * 1024 + 1)
        res = await claude_cli.spawn_claude(prompt=huge, model=None)
        # Guarded BEFORE spawning, so the fake binary never runs.
        assert res.error_kind is ErrorKind.CONTEXT
        assert "10 MiB" in res.error_message


class TestForwardCompatExtraction:
    def _write(self, tmp_path, messages):
        slug_dir = tmp_path / "-proj"
        slug_dir.mkdir()
        f = slug_dir / "sess.jsonl"
        f.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
        return str(f)

    def test_unknown_top_level_type_is_captured(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "user", "sessionId": "s", "message": {"content": "hi"}},
            {"type": "agent_team_update", "sessionId": "s",
             "message": {"content": "spawned 3 subagents to refactor auth"}},
        ])
        d = extractor.extract_session(path)
        assert "agent_team_update" in [t.role for t in d.timeline]
        text = extractor.digest_to_text(d)
        assert "spawned 3 subagents" in text
        assert "AGENT_TEAM_UPDATE" in text

    def test_unknown_assistant_block_is_marked(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "assistant", "sessionId": "s", "message": {"content": [
                {"type": "text", "text": "done"},
                {"type": "some_future_block", "data": "xyz"},
            ]}},
        ])
        d = extractor.extract_session(path)
        actions = [a for t in d.timeline for a in t.tool_actions]
        assert any("some_future_block" in a for a in actions)

    def test_new_tool_name_is_rendered(self, tmp_path):
        path = self._write(tmp_path, [
            {"type": "assistant", "sessionId": "s", "message": {"content": [
                {"type": "tool_use", "name": "Workflow",
                 "input": {"name": "diagnose-timeouts"}},
            ]}},
        ])
        d = extractor.extract_session(path)
        actions = [a for t in d.timeline for a in t.tool_actions]
        assert any("Workflow" in a and "diagnose-timeouts" in a for a in actions)
