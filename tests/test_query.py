"""Tests for chronicle.query — search, timeline, sessions, projects, and bare project lookup."""

import os
import textwrap
from pathlib import Path

import pytest

from chronicle import query


@pytest.fixture
def chronicle_dirs(tmp_path, monkeypatch):
    """Set up a fake PROJECTS_DIR with two projects and sample sessions."""
    projects_dir = tmp_path / "projects"
    monkeypatch.setattr(query, "PROJECTS_DIR", projects_dir)

    # Project: home-synesis-medium (2 sessions)
    medium_sessions = projects_dir / "home-synesis-medium" / "sessions"
    medium_sessions.mkdir(parents=True)

    (medium_sessions / "abc123.md").write_text(textwrap.dedent("""\
        # Session: Refactored auth middleware

        **Date**: 2026-03-28 | **Duration**: 45min

        ## Summary

        Replaced JWT validation with custom middleware.

        ### Switched from express-jwt to custom middleware

        Dropped express-jwt dependency.

        ### Added rate limiting per user

        Sliding window rate limiter tied to user ID.
    """))

    (medium_sessions / "def456.md").write_text(textwrap.dedent("""\
        # Session: Database migration to PostgreSQL

        **Date**: 2026-04-01 | **Duration**: 1h20min

        ## Summary

        Migrated from SQLite to PostgreSQL for production readiness.

        ### Chose pgBouncer for connection pooling

        Evaluated pgpool-II vs pgBouncer.
    """))

    # Project: home-synesis-claudetalktoclaude (1 session)
    ctc_sessions = projects_dir / "home-synesis-claudetalktoclaude" / "sessions"
    ctc_sessions.mkdir(parents=True)

    (ctc_sessions / "ghi789.md").write_text(textwrap.dedent("""\
        # Session: Added query shorthand

        **Date**: 2026-04-02 | **Duration**: 20min

        ## Summary

        Fixed chronicle query to accept bare project names.

        ### Bare project name as query shorthand

        Users can type project name directly.
    """))

    return projects_dir


class TestShowProject:
    def test_finds_project_by_partial_name(self, chronicle_dirs, capsys):
        query.show_project("medium")
        out = capsys.readouterr().out
        assert "home-synesis-medium" in out
        assert "2 sessions" in out
        assert "Refactored auth middleware" in out
        assert "Database migration to PostgreSQL" in out

    def test_no_match(self, chronicle_dirs, capsys):
        query.show_project("nonexistent")
        out = capsys.readouterr().out
        assert "No chronicles found matching 'nonexistent'" in out

    def test_multiple_matches(self, chronicle_dirs, capsys):
        query.show_project("home-synesis")
        out = capsys.readouterr().out
        assert "home-synesis-medium" in out
        assert "home-synesis-claudetalktoclaude" in out

    def test_no_projects_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(query, "PROJECTS_DIR", tmp_path / "nope")
        query.show_project("anything")
        out = capsys.readouterr().out
        assert "No chronicles found" in out


class TestSearch:
    def test_finds_matching_text(self, chronicle_dirs, capsys):
        query.search("PostgreSQL")
        out = capsys.readouterr().out
        assert "PostgreSQL" in out
        assert "match" in out

    def test_case_insensitive(self, chronicle_dirs, capsys):
        query.search("postgresql")
        out = capsys.readouterr().out
        assert "match" in out

    def test_no_results(self, chronicle_dirs, capsys):
        query.search("xyznonexistent")
        out = capsys.readouterr().out
        assert "No results" in out

    def test_filter_by_project(self, chronicle_dirs, capsys):
        query.search("Session", project="medium")
        out = capsys.readouterr().out
        assert "medium" in out
        assert "claudetalktoclaude" not in out

    def test_no_projects_dir(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(query, "PROJECTS_DIR", tmp_path / "nope")
        query.search("anything")
        out = capsys.readouterr().out
        assert "No chronicles found" in out


class TestTimeline:
    def test_shows_all_sessions_sorted(self, chronicle_dirs, capsys):
        query.timeline(limit=20)
        out = capsys.readouterr().out
        assert "3 of 3" in out
        # Newest first
        lines = out.split("\n")
        dates = [l.strip() for l in lines if l.strip().startswith("[")]
        assert len(dates) == 3
        assert "2026-04-02" in dates[0]
        assert "2026-04-01" in dates[1]
        assert "2026-03-28" in dates[2]

    def test_limit(self, chronicle_dirs, capsys):
        query.timeline(limit=1)
        out = capsys.readouterr().out
        assert "1 of 3" in out
        assert "2026-04-02" in out
        assert "2026-03-28" not in out

    def test_filter_by_project(self, chronicle_dirs, capsys):
        query.timeline(project="medium")
        out = capsys.readouterr().out
        assert "2 of 2" in out
        assert "claudetalktoclaude" not in out

    def test_no_sessions(self, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "projects" / "empty" / "sessions"
        empty.mkdir(parents=True)
        monkeypatch.setattr(query, "PROJECTS_DIR", tmp_path / "projects")
        query.timeline()
        out = capsys.readouterr().out
        assert "No session records" in out


class TestListProjects:
    def test_lists_projects_with_counts(self, chronicle_dirs, capsys):
        query.list_projects()
        out = capsys.readouterr().out
        assert "home-synesis-medium: 2 sessions" in out
        assert "home-synesis-claudetalktoclaude: 1 sessions" in out


class TestMainArgParsing:
    """Test that the main() entrypoint routes bare project names correctly."""

    def test_bare_name_routes_to_show_project(self, chronicle_dirs, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["chronicle.query", "medium"])
        query.main()
        out = capsys.readouterr().out
        assert "home-synesis-medium" in out
        assert "2 sessions" in out

    def test_subcommand_still_works(self, chronicle_dirs, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["chronicle.query", "projects"])
        query.main()
        out = capsys.readouterr().out
        assert "home-synesis-medium" in out

    def test_search_subcommand(self, chronicle_dirs, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["chronicle.query", "search", "PostgreSQL"])
        query.main()
        out = capsys.readouterr().out
        assert "match" in out


class TestVersion:
    def test_version_string_exists(self):
        from chronicle import __version__
        assert __version__
        assert "." in __version__

    def test_version_matches_pyproject(self):
        from chronicle import __version__
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text()
        assert f'version = "{__version__}"' in content
