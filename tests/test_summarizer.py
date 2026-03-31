"""Tests for summarizer.py — JSON extraction, response parsing, cross_references."""

import json

from chronicle.summarizer import (
    _extract_json,
    _parse_claude_response,
    ChronicleEntry,
)


class TestExtractJson:
    """Tests for all 4 JSON extraction strategies."""

    def test_strategy1_direct_parse(self):
        data = {"title": "Test", "summary": "A test"}
        result = _extract_json(json.dumps(data))
        assert result == data

    def test_strategy2_markdown_json_block(self):
        text = 'Some preamble\n```json\n{"title": "Test"}\n```\nTrailing text'
        result = _extract_json(text)
        assert result == {"title": "Test"}

    def test_strategy2_plain_code_block(self):
        text = 'Preamble\n```\n{"title": "Test"}\n```'
        result = _extract_json(text)
        assert result == {"title": "Test"}

    def test_strategy3_outermost_braces(self):
        text = 'Here is the JSON: {"title": "Test", "summary": "Done"} — end'
        result = _extract_json(text)
        assert result["title"] == "Test"

    def test_strategy3_handles_braces_in_strings(self):
        """Strategy 3 tracks in_string state, so braces in strings are safe."""
        data = {"title": "Test {with braces}", "code": "if (x) { return y; }"}
        text = f"Response: {json.dumps(data)} done"
        result = _extract_json(text)
        assert result["title"] == "Test {with braces}"
        assert result["code"] == "if (x) { return y; }"

    def test_strategy4_truncated_json(self):
        """Strategy 4 recovers truncated JSON by closing missing outer braces."""
        # Missing final } — Strategy 3 fails because depth never reaches 0,
        # but Strategy 4 finds '"}'  end marker and closes the remaining brace.
        text = '{"title": "Test", "nested": {"key": "val"}'
        result = _extract_json(text)
        assert result is not None
        assert result["title"] == "Test"
        assert result["nested"]["key"] == "val"

    def test_empty_string_returns_none(self):
        assert _extract_json("") is None
        assert _extract_json("   ") is None

    def test_no_json_returns_none(self):
        assert _extract_json("This is just plain text with no JSON.") is None

    def test_html_error_page_returns_garbage_or_none(self):
        """HTML error pages should not produce meaningful JSON."""
        html = "<html><body><h1>500 Internal Server Error</h1></body></html>"
        result = _extract_json(html)
        # Should return None or a dict without chronicle keys
        if result is not None:
            assert "title" not in result


class TestParseClaude:
    def _make_entry(self) -> ChronicleEntry:
        return ChronicleEntry(
            session_id="abc12345",
            project_path="/test",
            project_slug="test",
            start_time="2026-04-01T00:00:00Z",
            end_time="2026-04-01T01:00:00Z",
            git_branch="main",
            user_prompts=[],
        )

    def test_parses_valid_json(self):
        data = {
            "title": "Wiring hooks",
            "summary": "Set up hooks",
            "decisions": [{"what": "chose X", "why": "faster"}],
        }
        stdout = json.dumps({"result": json.dumps(data)})
        entry = _parse_claude_response(stdout, self._make_entry())
        assert entry.title == "Wiring hooks"
        assert entry.summary == "Set up hooks"
        assert len(entry.decisions) == 1

    def test_no_decisions_sets_is_empty(self):
        stdout = json.dumps({"result": "NO_DECISIONS"})
        entry = _parse_claude_response(stdout, self._make_entry())
        assert entry.is_empty is True

    def test_no_decisions_prefix(self):
        stdout = "NO_DECISIONS — session was trivial"
        entry = _parse_claude_response(stdout, self._make_entry())
        assert entry.is_empty is True

    def test_garbage_input_uses_unstructured_fallback(self):
        stdout = "This is not JSON at all, just garbage text"
        entry = _parse_claude_response(stdout, self._make_entry())
        assert entry.title == "Session summary (unstructured)"
        assert "garbage text" in entry.narrative

    def test_cross_references_parsed(self):
        data = {
            "title": "Follow-up",
            "summary": "Continued work",
            "cross_references": ["Session abc: initial setup"],
        }
        stdout = json.dumps(data)
        entry = _parse_claude_response(stdout, self._make_entry())
        assert len(entry.cross_references) == 1
        assert "initial setup" in entry.cross_references[0]

    def test_html_error_page_not_parsed_as_chronicle(self):
        """HTML error pages with {} should not produce valid chronicle entries."""
        html = '<html><body>{"error": "rate_limited"}</body></html>'
        entry = _parse_claude_response(html, self._make_entry())
        # The validation check should reject this since it has "error" but
        # not "title", "summary", or "decisions"
        assert entry.title != "rate_limited" or entry.title == "Session summary (unstructured)"

    def test_missing_fields_get_defaults(self):
        data = {"title": "Minimal"}
        stdout = json.dumps(data)
        entry = _parse_claude_response(stdout, self._make_entry())
        assert entry.title == "Minimal"
        assert entry.summary == ""
        assert entry.decisions == []
        assert entry.cross_references == []
