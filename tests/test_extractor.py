"""Tests for extractor.py — secret redaction, tool extraction, tag stripping."""

from chronicle.extractor import (
    _redact_secrets,
    _extract_tool,
    _is_real_user_prompt,
    _SYSTEM_TAG_PATTERN,
)


class TestRedactSecrets:
    def test_redacts_api_key(self):
        text = "API_KEY=sk-abc123xyz"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test.sig"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_aws_key(self):
        text = "key = AKIAIOSFODNN7EXAMPLE"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_github_pat(self):
        text = "token: github_pat_11AAAAAA0xxxxxxxxxxxxxxxxxxxxxxx"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_pem_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIBog...\n-----END RSA PRIVATE KEY-----"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_jwt(self):
        text = "token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        assert "[REDACTED]" in _redact_secrets(text)

    def test_redacts_db_url(self):
        text = "DATABASE_URL=postgres://user:pass@host:5432/db"
        result = _redact_secrets(text)
        assert "pass" not in result

    def test_preserves_benign_text(self):
        text = "This is a normal code comment about authentication flow"
        assert _redact_secrets(text) == text

    def test_empty_input(self):
        assert _redact_secrets("") == ""
        assert _redact_secrets(None) is None


class TestExtractTool:
    def test_bash_tool(self):
        block = {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}
        summary, detail = _extract_tool(block)
        assert "Bash" in summary
        assert "ls -la" in detail.command

    def test_edit_tool(self):
        block = {
            "type": "tool_use", "name": "Edit",
            "input": {"file_path": "/foo/bar.py", "old_string": "x", "new_string": "y"},
        }
        summary, detail = _extract_tool(block)
        assert "Edit" in summary
        assert detail.path == "/foo/bar.py"

    def test_multiedit_tool(self):
        block = {
            "type": "tool_use", "name": "MultiEdit",
            "input": {
                "file_path": "/foo/bar.py",
                "edits": [{"old": "a", "new": "b"}, {"old": "c", "new": "d"}],
            },
        }
        summary, detail = _extract_tool(block)
        assert "MultiEdit" in summary
        assert "2 regions" in summary
        assert detail.path == "/foo/bar.py"

    def test_write_tool_redacts_sensitive(self):
        block = {
            "type": "tool_use", "name": "Write",
            "input": {"file_path": "/home/user/.env", "content": "SECRET=abc123"},
        }
        summary, detail = _extract_tool(block)
        assert "REDACTED" in detail.content

    def test_non_tool_use_returns_none(self):
        block = {"type": "text", "text": "hello"}
        summary, detail = _extract_tool(block)
        assert summary is None
        assert detail is None

    def test_unknown_tool_fallback(self):
        block = {
            "type": "tool_use", "name": "FutureTool",
            "input": {"query": "test query"},
        }
        summary, detail = _extract_tool(block)
        assert "FutureTool" in summary
        assert "test query" in summary


class TestIsRealUserPrompt:
    def test_real_prompt(self):
        assert _is_real_user_prompt("Fix the bug in auth.py") is True

    def test_system_reminder(self):
        assert _is_real_user_prompt("<system-reminder>You have tools</system-reminder>") is False

    def test_empty(self):
        assert _is_real_user_prompt("") is False
        assert _is_real_user_prompt("   ") is False

    def test_local_command(self):
        assert _is_real_user_prompt("<local-command-stdout>output here</local-command-stdout>") is False


class TestSystemTagStripping:
    def test_strips_system_reminder(self):
        text = "Hello <system-reminder>internal</system-reminder> world"
        result = _SYSTEM_TAG_PATTERN.sub("", text)
        assert result == "Hello internal world"

    def test_preserves_user_html(self):
        text = "Use <div class='foo'>bar</div> for layout"
        result = _SYSTEM_TAG_PATTERN.sub("", text)
        assert "<div" in result  # user HTML preserved

    def test_preserves_angle_brackets(self):
        text = "if x < 10 and y > 5:"
        result = _SYSTEM_TAG_PATTERN.sub("", text)
        assert result == text
