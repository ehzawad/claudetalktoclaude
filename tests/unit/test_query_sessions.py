"""Unit test for the bug where `chronicle query sessions` printed a
recovery command using the raw filesystem path instead of the Claude
project slug — making the suggested `chronicle process --project <path>`
find zero matches.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def isolated_query(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle" / "projects").mkdir(parents=True)
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    import importlib
    for mod in ("chronicle.config", "chronicle.mode", "chronicle.storage",
                "chronicle.query", "chronicle.daemon"):
        importlib.reload(__import__(mod, fromlist=["_"]))
    yield fake_home


def test_suggested_command_uses_slug_not_raw_path(
    isolated_query, tmp_path, monkeypatch, capsys,
):
    """If the cwd has unprocessed JSONLs, the printed recovery command
    must use the slugged project name (substring-matches batch's filter),
    NOT the raw filesystem path (won't match because slashes vs dashes).
    """
    fake_home = isolated_query
    # Seed a Claude Code project dir for cwd=/my/project/foo
    project_cwd = tmp_path / "my" / "project" / "foo"
    project_cwd.mkdir(parents=True)
    slug = str(project_cwd).replace("/", "-")
    claude_proj = fake_home / ".claude" / "projects" / slug
    claude_proj.mkdir(parents=True)
    (claude_proj / "abc-123.jsonl").write_text('{"type":"user"}\n')

    # Monkeypatch os.getcwd to return our fake cwd
    monkeypatch.setattr("os.getcwd", lambda: str(project_cwd))

    from chronicle import query
    query.sessions()
    captured = capsys.readouterr().out

    # Must print the slug, NOT the raw path, as the --project value
    assert f"--project {slug}" in captured
    # Sanity: the raw slashed path should NOT appear in the suggested command line
    for line in captured.splitlines():
        if "chronicle process --project" in line:
            assert str(project_cwd) not in line, (
                f"raw path leaked into suggested command: {line!r}"
            )


def test_suggestion_is_substring_of_slug(isolated_query, tmp_path, monkeypatch, capsys):
    """batch.find_all_sessions substring-matches `--project` against
    slugged directory names. The suggested value must be a substring of
    at least one such directory; we assert by direct re-use.
    """
    fake_home = isolated_query
    project_cwd = tmp_path / "demo-proj"
    project_cwd.mkdir()
    slug = str(project_cwd).replace("/", "-")
    claude_proj = fake_home / ".claude" / "projects" / slug
    claude_proj.mkdir(parents=True)
    (claude_proj / "sess.jsonl").write_text('{}\n')
    monkeypatch.setattr("os.getcwd", lambda: str(project_cwd))

    from chronicle import query
    query.sessions()
    captured = capsys.readouterr().out
    # extract the project filter from the suggested command
    for line in captured.splitlines():
        if "chronicle process --project" in line:
            parts = line.strip().split()
            assert "--project" in parts
            idx = parts.index("--project")
            suggested = parts[idx + 1]
            # The suggestion must substring-match the actual directory
            # name (simulating batch.find_all_sessions behavior).
            assert suggested in claude_proj.name, (
                f"suggested={suggested!r} not a substring of slug={claude_proj.name!r}"
            )
            return
    pytest.fail("no `chronicle process --project ...` line in output")
