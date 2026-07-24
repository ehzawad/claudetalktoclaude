"""_extract_structured must not throw away a usable summary.

Claude Code 2.1.219 can exit 0 with is_error=false and NO structured_output
key, even when --json-schema was passed — measured across repeated live calls.
When that happens because the model emitted the JSON as ordinary assistant text
instead of invoking the structured-output tool, the payload is in `result`.

This is defensive parsing of CURRENT CLI behavior, not compatibility with an
old one, so it must survive future legacy sweeps.
"""
from __future__ import annotations

import json

from chronicle.summarizer import _extract_structured


PAYLOAD = {"is_empty": False, "title": "Wire up hooks", "summary": "Did the thing."}


def test_structured_output_is_used_when_present():
    outer = {"structured_output": PAYLOAD, "result": "ignored prose"}
    assert _extract_structured(outer) == PAYLOAD


def test_structured_output_wins_over_result_when_both_present():
    other = {"is_empty": True, "title": "Different"}
    outer = {"structured_output": PAYLOAD, "result": json.dumps(other)}
    assert _extract_structured(outer) == PAYLOAD


def test_json_object_in_result_is_recovered_when_structured_output_missing():
    """The regression this file exists for: without this, a complete
    summarization is discarded as ErrorKind.PARSE."""
    outer = {"result": json.dumps(PAYLOAD)}
    assert _extract_structured(outer) == PAYLOAD


def test_json_object_in_result_is_recovered_when_structured_output_is_null():
    outer = {"structured_output": None, "result": json.dumps(PAYLOAD)}
    assert _extract_structured(outer) == PAYLOAD


def test_result_with_surrounding_whitespace_is_recovered():
    outer = {"result": "\n  " + json.dumps(PAYLOAD) + "  \n"}
    assert _extract_structured(outer) == PAYLOAD


def test_prose_result_is_not_mistaken_for_structured_output():
    """The two live no-key responses observed were prose — must stay a failure."""
    outer = {"result": "I summarized the session for you: it was about hooks."}
    assert _extract_structured(outer) is None


def test_non_object_json_in_result_is_rejected():
    for raw in ("[1, 2, 3]", '"a string"', "42", "true", "null"):
        assert _extract_structured({"result": raw}) is None, raw


def test_empty_result_is_none():
    for raw in ("", "   ", None):
        assert _extract_structured({"result": raw}) is None


def test_missing_both_keys_is_none():
    assert _extract_structured({}) is None
