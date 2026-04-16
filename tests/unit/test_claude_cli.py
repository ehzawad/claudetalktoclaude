"""Unit tests for chronicle.claude_cli."""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
from pathlib import Path

import pytest

from chronicle import claude_cli


@pytest.fixture(autouse=True)
def _reset_cache():
    claude_cli._reset_cache_for_tests()
    yield
    claude_cli._reset_cache_for_tests()


def _make_stub_claude(dest: Path, script_body: str = "print('{\"ok\": true}')") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"#!/usr/bin/env python3\n{script_body}\n")
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest


class TestResolveClaudeBinary:
    def test_finds_via_path(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        stub = _make_stub_claude(bin_dir / "claude")
        monkeypatch.setenv("PATH", str(bin_dir))
        resolved = claude_cli.resolve_claude_binary()
        assert resolved == stub.resolve()

    def test_falls_back_to_home_local_bin(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        stub = _make_stub_claude(fake_home / ".local" / "bin" / "claude")
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")  # No ~/.local/bin
        resolved = claude_cli.resolve_claude_binary()
        assert resolved == stub.resolve()

    def test_raises_when_absent(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(claude_cli.ClaudeNotFound):
            claude_cli.resolve_claude_binary()

    def test_try_resolve_returns_none_when_absent(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert claude_cli.try_resolve_claude_binary() is None

    def test_caches_result(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(bin_dir / "claude")
        monkeypatch.setenv("PATH", str(bin_dir))
        first = claude_cli.resolve_claude_binary()
        # Change PATH — cached value should stick
        monkeypatch.setenv("PATH", "/usr/bin")
        second = claude_cli.resolve_claude_binary()
        assert first == second

    def test_force_refresh_rescans(self, tmp_path, monkeypatch):
        # Isolated HOME so fallback dirs (~/.local/bin) don't leak real claude
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        bin_dir = tmp_path / "bin"
        _make_stub_claude(bin_dir / "claude")
        monkeypatch.setenv("PATH", str(bin_dir))
        claude_cli.resolve_claude_binary()
        (bin_dir / "claude").unlink()
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(claude_cli.ClaudeNotFound):
            claude_cli.resolve_claude_binary(force_refresh=True)


class TestBuildSubprocessEnv:
    def test_strips_all_three_vars(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-foo")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "Bearer-foo")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example")
        env = claude_cli.build_subprocess_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert "ANTHROPIC_BASE_URL" not in env

    def test_preserves_user_vars(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "keep-me")
        env = claude_cli.build_subprocess_env()
        assert env.get("MY_VAR") == "keep-me"

    def test_path_includes_standard_dirs_even_if_source_path_empty(self, monkeypatch):
        monkeypatch.setenv("PATH", "")
        env = claude_cli.build_subprocess_env()
        path_parts = env["PATH"].split(os.pathsep)
        # At least system dirs should appear (they exist on macOS + Linux)
        assert "/usr/bin" in path_parts or "/bin" in path_parts

    def test_from_explicit_base(self, tmp_path):
        base = {"PATH": "/custom/bin", "ANTHROPIC_API_KEY": "sk-x", "FOO": "bar"}
        env = claude_cli.build_subprocess_env(base=base)
        assert "ANTHROPIC_API_KEY" not in env
        assert env["FOO"] == "bar"
        assert "/custom/bin" in env["PATH"].split(os.pathsep)


class TestSpawnClaude:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_json(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(
            bin_dir / "claude",
            'import sys, json; sys.stdin.read();'
            ' print(json.dumps({"total_cost_usd": 0.02, "structured_output": {"ok": True}}))',
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        result = await claude_cli.spawn_claude(
            prompt="hello", model="opus", fallback_model="sonnet",
        )
        assert result.ok
        assert result.stdout_json["structured_output"] == {"ok": True}
        assert result.total_cost_usd == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_missing_binary_is_infra(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
        result = await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet",
        )
        assert not result.ok
        assert result.error_kind == claude_cli.ErrorKind.INFRA

    @pytest.mark.asyncio
    async def test_claude_is_error_is_transient(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(
            bin_dir / "claude",
            'import sys, json; sys.stdin.read();'
            ' print(json.dumps({"is_error": True, "result": "boom", "total_cost_usd": 0}))',
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        result = await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet",
        )
        assert result.error_kind == claude_cli.ErrorKind.TRANSIENT

    @pytest.mark.asyncio
    async def test_parse_failure_is_parse_kind(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(bin_dir / "claude", 'import sys; sys.stdin.read(); print("not json")')
        monkeypatch.setenv("PATH", str(bin_dir))
        result = await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet",
        )
        assert result.error_kind == claude_cli.ErrorKind.PARSE

    @pytest.mark.asyncio
    async def test_timeout_is_transient(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(
            bin_dir / "claude",
            "import time; time.sleep(60)",
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        result = await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet", timeout=0.5,
        )
        assert result.error_kind == claude_cli.ErrorKind.TRANSIENT
        assert "timed out" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_auth_hint_classified_as_infra(
        self, tmp_path, monkeypatch,
    ):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(
            bin_dir / "claude",
            'import sys; sys.stdin.read();'
            ' sys.stderr.write("Authentication required: please log in"); sys.exit(1)',
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        result = await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet",
        )
        assert result.error_kind == claude_cli.ErrorKind.INFRA

    @pytest.mark.asyncio
    async def test_subprocess_registry_cleared_after_completion(
        self, tmp_path, monkeypatch,
    ):
        bin_dir = tmp_path / "bin"
        _make_stub_claude(
            bin_dir / "claude",
            'import sys, json; sys.stdin.read(); print(json.dumps({"structured_output": {}}))',
        )
        monkeypatch.setenv("PATH", str(bin_dir))
        assert claude_cli.active_subprocess_count() == 0
        await claude_cli.spawn_claude(
            prompt="x", model="opus", fallback_model="sonnet",
        )
        assert claude_cli.active_subprocess_count() == 0
