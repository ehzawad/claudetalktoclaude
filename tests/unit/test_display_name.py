"""Unit tests for the project display-name helpers.

These lock the fix for project names that used to render as the raw
leading-dash slug ('-Users-ehz-codex-council') or the lossy last segment
('codex-council' -> 'council').
"""
import pytest

from chronicle.config import (
    project_display_name,
    project_name_matches,
    recover_project_path,
)


@pytest.mark.parametrize("slug,path,expected", [
    # slug-only: strip exactly ONE leading dash, never lstrip-all
    ("-Users-ehz-codex-council", None, "Users-ehz-codex-council"),
    ("-Users-x--config-nvim", None, "Users-x--config-nvim"),   # double dash preserved
    ("--config", None, "-config"),                              # only one dash stripped
    ("-", None, "-"),                                           # never empty
    ("", None, "(unknown project)"),                            # degraded fallback
    # path known: true basename, correct even with '-', '.', '_'
    ("-Users-x-codex-council", "/Users/x/codex-council", "codex-council"),
    ("-Users-x-my-proj", "/Users/x/my_proj", "my_proj"),
    ("--config", "/.config", ".config"),
    ("-a-b", "/a/b/", "b"),                                     # trailing slash
    ("-", "/", "-"),                                            # root has no basename
    # path that is not really a path must NOT become its own basename
    ("-Users-x-proj", "-Users-x-proj", "Users-x-proj"),        # slug mis-passed as path
    ("-fallback", "", "fallback"),
    ("-fallback", ".", "fallback"),                             # relative, no separator
])
def test_project_display_name(slug, path, expected):
    assert project_display_name(slug, path) == expected


@pytest.mark.parametrize("query,slug,expected", [
    ("codex-council", "-Users-x-codex-council", True),         # plain substring
    ("my_proj", "-Users-x-my-proj", True),                     # underscore -> dash
    (".config", "-Users-x--config", True),                     # dot -> dash
    ("Project Alpha", "-Users-x-Project-Alpha", True),         # space -> dash
    ("nope", "-Users-x-foo", False),
    # punctuation-only queries must NOT match every slug (wrong-project bug)
    (".", "-Users-x-codex-council", False),
    ("/", "-Users-x-codex-council", False),
    ("_", "-Users-x-codex-council", False),
    ("   ", "-Users-x-codex-council", False),
    ("", "-Users-x-codex-council", False),
])
def test_project_name_matches(query, slug, expected):
    assert project_name_matches(query, slug) is expected


def test_recover_project_path_rejects_foreign_path(tmp_path):
    # A copied/restored record under one slug carrying another project's cwd
    # must NOT be trusted (would display the wrong basename).
    pdir = tmp_path / "Users-me-api"   # chronicle dir = de-dashed storage key
    sessions = pdir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.md").write_text("# S\n\n**Project**: /Users/me/client\n")
    assert recover_project_path(pdir) is None
    # display falls back to the de-dashed slug, not 'client'
    assert project_display_name(pdir.name, recover_project_path(pdir)) == "Users-me-api"


def test_recover_project_path_reads_newest_record(tmp_path):
    pdir = tmp_path / "Users-x-proj"   # de-dashed storage key
    sessions = pdir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "2026-01-01-old.md").write_text(
        "# Old\n\n**Project**: /Users/x/old-path\n")
    (sessions / "2026-05-01-new.md").write_text(
        "# New\n\n**Session**: abc | **Date**: 2026-05-01\n"
        "**Project**: /Users/x/proj\n")
    # newest filename sorts last; reverse=True picks it first
    assert recover_project_path(pdir) == "/Users/x/proj"


def test_recover_project_path_none_when_no_records(tmp_path):
    pdir = tmp_path / "Users-x-empty"
    (pdir / "sessions").mkdir(parents=True)
    assert recover_project_path(pdir) is None
    # no sessions dir at all
    assert recover_project_path(tmp_path / "Users-x-missing") is None


def test_recover_project_path_empty_value_does_not_grab_next_line(tmp_path):
    # A transcript with no cwd writes a bare "**Project**: " (trailing space).
    # The recovery regex must NOT consume the newline and capture "## Summary".
    pdir = tmp_path / "Users-x-nocwd"
    sessions = pdir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.md").write_text(
        "# S\n\n**Session**: abc | **Date**: 2026-05-01\n"
        "**Project**: \n\n## Summary\n\nstuff\n")
    assert recover_project_path(pdir) is None
    # and the display falls back to the de-dashed slug, never "## Summary"
    assert project_display_name(pdir.name, recover_project_path(pdir)) == "Users-x-nocwd"


def test_recover_then_display_gives_basename(tmp_path):
    pdir = tmp_path / "Users-x-codex-council"
    sessions = pdir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s.md").write_text("# S\n\n**Project**: /Users/x/codex-council\n")
    assert project_display_name(pdir.name, recover_project_path(pdir)) == "codex-council"


# ---------- chronicle.md header backfill ----------

def test_repair_header_fixes_lossy_and_is_idempotent():
    from chronicle.storage import _repair_chronicle_header
    slug = "-Users-x-codex-council"
    content = "# Chronicle: council\n\nbody\n"            # old lossy header
    out = _repair_chronicle_header(content, slug, "/Users/x/codex-council")
    assert out.startswith("# Chronicle: codex-council\n")
    assert "body" in out
    # idempotent — second pass changes nothing
    assert _repair_chronicle_header(out, slug, "/Users/x/codex-council") == out


def test_repair_header_preserves_hand_edits():
    from chronicle.storage import _repair_chronicle_header
    slug = "-Users-x-codex-council"
    content = "# Chronicle: My Cool Project\n\nbody\n"     # not the lossy value
    assert _repair_chronicle_header(content, slug, "/Users/x/codex-council") == content


def test_repair_header_noop_when_not_a_chronicle_header():
    from chronicle.storage import _repair_chronicle_header
    slug = "-Users-x-codex-council"
    content = "# Something else\n\nbody\n"
    assert _repair_chronicle_header(content, slug, "/Users/x/codex-council") == content
