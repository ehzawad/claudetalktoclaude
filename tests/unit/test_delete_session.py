"""Regression: delete_session crashed AFTER unlinking the session file.

`full_id` was referenced but never bound in delete_session's scope, so
`chronicle rewind --delete N` (and `--prune`) raised NameError right after
`session_path.unlink()` — the markdown was gone, but the .processed/.failed
markers were never cleared and the prompts section was never rebuilt, leaving
the project in a half-deleted state that no command could repair.
"""
from __future__ import annotations

import pytest

from chronicle.config import processed_dir, project_chronicle_dir
from chronicle.storage import delete_session, mark_succeeded


FULL_ID = "abc12345-1111-2222-3333-444455556666"
SHORT_ID = FULL_ID[:8]
SLUG = "Users-x-demo"


def _seed(tmp_path, monkeypatch, *, with_marker: bool = True):
    monkeypatch.setenv("CHRONICLE_HOME", str(tmp_path))
    pdir = project_chronicle_dir(SLUG)
    (pdir / "sessions").mkdir(parents=True, exist_ok=True)
    session = pdir / "sessions" / f"2026-01-01_0000_{SHORT_ID}_demo.md"
    session.write_text(
        f"# Demo session\n\n**Session**: {SHORT_ID} | **Date**: 2026-01-01\n\n"
        "## Summary\n\nDid a thing.\n"
    )
    body = "# Chronicle: demo\n\n"
    if with_marker:
        body += f"## 2026-01-01 | Demo session\n<!-- session:{FULL_ID} -->\n\nDid a thing.\n"
    (pdir / "chronicle.md").write_text(body)
    return session


def test_delete_session_clears_markers_and_does_not_raise(tmp_path, monkeypatch):
    session = _seed(tmp_path, monkeypatch)
    mark_succeeded(FULL_ID, "2026-01-01T00:00:00Z")
    assert list(processed_dir().glob("*")), "precondition: a success marker exists"

    delete_session(session, SLUG)

    assert not session.exists()
    assert not list(processed_dir().glob("*")), (
        "markers must be cleared; the full UUID is recovered from the chronicle.md marker"
    )
    assert f"session:{FULL_ID}" not in (project_chronicle_dir(SLUG) / "chronicle.md").read_text()


def test_delete_session_without_a_chronicle_marker_still_completes(tmp_path, monkeypatch):
    """No marker to recover the full UUID from — must fall back to the short id."""
    session = _seed(tmp_path, monkeypatch, with_marker=False)
    mark_succeeded(SHORT_ID, "2026-01-01T00:00:00Z")

    delete_session(session, SLUG)

    assert not session.exists()
    assert not list(processed_dir().glob("*"))


def test_delete_session_missing_chronicle_file_is_not_fatal(tmp_path, monkeypatch):
    session = _seed(tmp_path, monkeypatch)
    (project_chronicle_dir(SLUG) / "chronicle.md").unlink()

    delete_session(session, SLUG)
    assert not session.exists()
