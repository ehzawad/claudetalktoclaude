"""Regressions against the current Claude Code programmatic CLI contract."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from chronicle import claude_cli


@pytest.fixture(autouse=True)
def _reset_claude_cache():
    claude_cli._reset_cache_for_tests()
    yield
    claude_cli._reset_cache_for_tests()


def _make_stub(dest: Path, body: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/usr/bin/env python3\n{body}\n")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest


@pytest.mark.asyncio
async def test_programmatic_calls_use_safe_mode_and_disable_tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _make_stub(
        bin_dir / "claude",
        "import json, sys; sys.stdin.read(); "
        "print(json.dumps({'structured_output': {'argv': sys.argv[1:]}}))",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    result = await claude_cli.spawn_claude(
        prompt="summarize this transcript",
        model="opus",
        fallback_model="sonnet",
        effort="high",
        json_schema={"type": "object"},
    )

    assert result.ok
    argv = result.stdout_json["structured_output"]["argv"]
    assert argv[:2] == ["--safe-mode", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--fallback-model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}


@pytest.mark.asyncio
async def test_old_cli_without_safe_mode_is_actionable_infra_error(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    _make_stub(
        bin_dir / "claude",
        "import sys; sys.stdin.read(); "
        "sys.stderr.write(\"error: unknown option '--safe-mode'\"); sys.exit(1)",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    result = await claude_cli.spawn_claude(prompt="x")

    assert result.error_kind is claude_cli.ErrorKind.INFRA
    assert "claude update" in result.error_message
    assert "--safe-mode" in result.error_message
