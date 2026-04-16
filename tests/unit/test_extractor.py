"""Unit tests for chronicle.extractor.

This module is security-critical: its secret-redaction regex is the last
line of defense between tokens/keys/credentials and on-disk markdown.
A silent regression here would leak secrets into the chronicle.

Also covers:
- JSONL parsing (user/assistant/tool_result timeline construction)
- Tool-use extraction (Bash/Edit/Write/Read/Agent/MCP)
- Tool result truncation at 10KB
- User-prompt filtering (skip system-injected tags)
- Sensitive file-path full-redaction (.env / .pem / .key)
- System XML stripping that preserves user-typed HTML
- Malformed JSON lines skipped gracefully
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chronicle import extractor


# ---------- Pure redaction ----------

class TestSecretRedaction:
    def test_api_key_sk(self):
        out = extractor._redact_secrets("auth key: sk-abc123def456")
        assert "sk-abc123def456" not in out
        assert "[REDACTED]" in out

    def test_github_token(self):
        out = extractor._redact_secrets("token=ghp_DEADBEEFcafe1234567890")
        assert "ghp_DEADBEEF" not in out
        assert "[REDACTED]" in out

    def test_aws_key(self):
        out = extractor._redact_secrets("cred AKIAIOSFODNN7EXAMPLE")
        assert "AKIA" not in out
        assert "[REDACTED]" in out

    def test_bearer_token(self):
        out = extractor._redact_secrets("Authorization: Bearer xyzPQR.abcDEF")
        assert "xyzPQR.abcDEF" not in out
        assert "[REDACTED]" in out

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.XYZ"
        out = extractor._redact_secrets(f"token: {jwt}")
        assert "eyJhbGciOiJIUzI1NiI" not in out
        assert "[REDACTED]" in out

    def test_postgres_connection_uri(self):
        out = extractor._redact_secrets(
            "url: postgres://user:hunter2@db.example.com:5432/mydb"
        )
        assert "hunter2" not in out
        assert "[REDACTED]" in out

    def test_api_key_env_var_assignment(self):
        out = extractor._redact_secrets("export API_KEY=somevalue")
        assert "somevalue" not in out

    def test_private_key_block(self):
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = extractor._redact_secrets(f"key:\n{block}")
        assert "MIIEpAIBAAKCAQEA" not in out
        assert "[REDACTED]" in out

    def test_plain_text_unchanged(self):
        """No false positives on harmless text."""
        harmless = "The CLI uses --model opus and --effort max."
        assert extractor._redact_secrets(harmless) == harmless


# ---------- Write tool — sensitive-path full redaction ----------

class TestSensitiveFilePathRedaction:
    def _make_write_block(self, path: str, content: str) -> dict:
        return {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": path, "content": content},
        }

    def test_env_file_fully_redacted(self):
        block = self._make_write_block(
            "/home/user/.env",
            "API_KEY=sk-real\nDATABASE_URL=postgres://u:p@h/db\n",
        )
        _, detail = extractor._extract_tool(block)
        assert detail is not None
        assert "[REDACTED" in detail.content
        assert "sk-real" not in detail.content
        assert "postgres://" not in detail.content

    def test_pem_file_fully_redacted(self):
        block = self._make_write_block(
            "/home/user/secret.pem",
            "-----BEGIN PRIVATE KEY-----\nbase64...\n-----END PRIVATE KEY-----",
        )
        _, detail = extractor._extract_tool(block)
        assert detail is not None
        assert "[REDACTED" in detail.content
        assert "BEGIN PRIVATE KEY" not in detail.content

    def test_non_sensitive_file_only_redacts_content_patterns(self):
        """A normal source file passes through the pattern scanner, not
        the full-redact path."""
        block = self._make_write_block(
            "/home/user/main.py",
            "def hello():\n    return 'world'\n# api key comment only",
        )
        _, detail = extractor._extract_tool(block)
        assert detail is not None
        # Full code preserved
        assert "def hello" in detail.content
        assert "return 'world'" in detail.content


# ---------- User-prompt filtering ----------

class TestUserPromptFiltering:
    def test_system_injected_prompt_skipped(self):
        assert not extractor._is_real_user_prompt("<system-reminder>hi</system-reminder>")
        assert not extractor._is_real_user_prompt("<local-command-stdout>")
        assert not extractor._is_real_user_prompt("<command-name>")
        assert not extractor._is_real_user_prompt(
            "[Request interrupted by user]\nMore text"
        )

    def test_real_user_prompt_kept(self):
        assert extractor._is_real_user_prompt("please fix the bug in main.py")

    def test_empty_prompt_skipped(self):
        assert not extractor._is_real_user_prompt("")
        assert not extractor._is_real_user_prompt("   \n\t  ")


# ---------- System-XML stripping preserves user HTML ----------

class TestSystemTagStripping:
    def test_strips_system_reminder_inside_text(self):
        text = "hello <system-reminder>noise</system-reminder> world"
        cleaned = extractor._SYSTEM_TAG_PATTERN.sub("", text)
        assert "<system-reminder>" not in cleaned
        assert "</system-reminder>" not in cleaned
        # But the inner "noise" is kept — only the tags get stripped
        assert "noise" in cleaned

    def test_preserves_user_typed_html(self):
        """Old implementation used re.sub(r'<[^>]+>', '') which ate user
        HTML. New pattern targets only known system tags."""
        user_text = "Look at this <div class='x'>tag</div> and <img src=foo/>"
        cleaned = extractor._SYSTEM_TAG_PATTERN.sub("", user_text)
        # User-typed HTML survives
        assert "<div class='x'>" in cleaned
        assert "</div>" in cleaned
        assert "<img src=foo/>" in cleaned


# ---------- Tool-result truncation ----------

class TestToolResultTruncation:
    def test_large_result_truncated_with_marker(self):
        big = "a" * (extractor._MAX_TOOL_RESULT_CHARS + 1000)
        out = extractor._extract_tool_result_text(big)
        assert out is not None
        assert "[... truncated ...]" in out
        assert len(out) < extractor._MAX_TOOL_RESULT_CHARS + 100

    def test_small_result_not_truncated(self):
        small = "short output"
        out = extractor._extract_tool_result_text(small)
        assert out == small

    def test_empty_result_returns_none(self):
        assert extractor._extract_tool_result_text("") is None
        assert extractor._extract_tool_result_text("   \n  ") is None


# ---------- JSONL parsing end-to-end ----------

class TestExtractSession:
    def _make_jsonl(self, dir_: Path, session_id: str, messages: list[dict]) -> Path:
        jsonl = dir_ / f"{session_id}.jsonl"
        jsonl.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
        return jsonl

    def test_basic_user_assistant_turn(self, tmp_path):
        sid = str(uuid.uuid4())
        proj = tmp_path / "-tmp-demo"
        proj.mkdir()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jsonl = self._make_jsonl(proj, sid, [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": now,
                "cwd": "/tmp/demo",
                "gitBranch": "main",
                "message": {"content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": sid,
                "timestamp": now,
                "message": {"content": [{"type": "text", "text": "hi"}]},
            },
        ])
        digest = extractor.extract_session(str(jsonl))
        assert digest.session_id == sid
        assert digest.project_slug == "-tmp-demo"
        assert len(digest.user_prompts) == 1
        assert digest.user_prompts[0].text == "hello"
        assert digest.assistant_responses == ["hi"]
        assert digest.total_turns == 2

    def test_malformed_line_skipped(self, tmp_path):
        sid = str(uuid.uuid4())
        proj = tmp_path / "-tmp-demo"
        proj.mkdir()
        jsonl = proj / f"{sid}.jsonl"
        jsonl.write_text(
            "not valid json\n"
            + json.dumps({
                "type": "user", "uuid": "u1", "sessionId": sid,
                "timestamp": "2026-04-17T00:00:00Z",
                "cwd": "/tmp", "gitBranch": "main",
                "message": {"content": "real prompt"},
            }) + "\n"
        )
        digest = extractor.extract_session(str(jsonl))
        # Malformed line dropped silently, valid line processed
        assert len(digest.user_prompts) == 1

    def test_redacts_secrets_in_bash_command(self, tmp_path):
        sid = str(uuid.uuid4())
        proj = tmp_path / "-tmp-demo"
        proj.mkdir()
        jsonl = self._make_jsonl(proj, sid, [
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": sid,
                "timestamp": "2026-04-17T00:00:00Z",
                "message": {"content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "curl -H 'Authorization: Bearer sk-LEAKED' api"},
                    }
                ]},
            },
        ])
        digest = extractor.extract_session(str(jsonl))
        # The one-liner action and the detail block must both be redacted
        assert all("sk-LEAKED" not in a for a in digest.tool_actions)
        assert all("Bearer sk-LEAKED" not in a for a in digest.tool_actions)
        for turn in digest.timeline:
            for d in turn.tool_details:
                if d.command:
                    assert "sk-LEAKED" not in d.command

    def test_system_injected_user_prompt_filtered(self, tmp_path):
        sid = str(uuid.uuid4())
        proj = tmp_path / "-tmp-demo"
        proj.mkdir()
        jsonl = self._make_jsonl(proj, sid, [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-04-17T00:00:00Z",
                "cwd": "/tmp", "gitBranch": "main",
                "message": {"content": "<system-reminder>boring</system-reminder>"},
            },
            {
                "type": "user",
                "uuid": "u2",
                "sessionId": sid,
                "timestamp": "2026-04-17T00:00:01Z",
                "cwd": "/tmp", "gitBranch": "main",
                "message": {"content": "real question here"},
            },
        ])
        digest = extractor.extract_session(str(jsonl))
        assert len(digest.user_prompts) == 1
        assert digest.user_prompts[0].text == "real question here"
