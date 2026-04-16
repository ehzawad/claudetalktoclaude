#!/usr/bin/env python3
"""Fake `claude` binary for Chronicle functional tests.

Installed on PATH as an executable named `claude`. Mimics the JSON output
shape of `claude -p --output-format json`. Behavior controlled by the
FAKE_CLAUDE_MODE env var:

  success        Valid structured_output, is_empty=false
  empty          Valid structured_output, is_empty=true
  error          JSON with is_error=true, exit 1 (claude's own error path)
  crash          Exit 2 with no stdout (subprocess returncode!=0 path)
  parse          stdout is not JSON (parse-fail path)
  timeout        Sleep 600s so parent wait_for(300) times out
  no-structured  Valid JSON without structured_output key (fallback to result)

Always drains stdin so the summarizer's prompt write doesn't deadlock.
"""
import json
import os
import sys
import time


_SUCCESS_STRUCTURED = {
    "is_empty": False,
    "title": "Fake test session",
    "summary": "stubbed summary from fake claude",
    "narrative": "stubbed narrative",
    "decisions": [],
    "problems_solved": [],
    "human_reasoning": [],
    "follow_ups": [],
    "technical_details": {},
    "architecture": {},
    "planning": {},
    "open_questions": [],
    "files_changed": [],
    "cross_references": [],
}


def _emit(payload: dict, exit_code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(exit_code)


def main() -> None:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

    try:
        sys.stdin.read()
    except Exception:
        pass

    if mode == "timeout":
        time.sleep(600)
        return

    if mode == "crash":
        sys.exit(2)

    if mode == "parse":
        sys.stdout.write("definitely not json")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "error":
        _emit({
            "total_cost_usd": 0.01,
            "is_error": True,
            "result": "fake claude reported error",
        }, exit_code=1)

    if mode == "empty":
        _emit({
            "total_cost_usd": 0.01,
            "is_error": False,
            "structured_output": {"is_empty": True, "title": "Fake empty session"},
        })

    if mode == "no-structured":
        _emit({
            "total_cost_usd": 0.01,
            "is_error": False,
            "result": "",
        })

    _emit({
        "total_cost_usd": 0.01,
        "is_error": False,
        "structured_output": _SUCCESS_STRUCTURED,
    })


if __name__ == "__main__":
    main()
