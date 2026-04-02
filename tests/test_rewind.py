"""Tests for rewind.py — session navigation, diff, since, session list."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from chronicle.rewind import (
    _load_sessions,
    show_session_list,
    show_session,
    show_since,
    show_diff,
)


def _make_session_file(sessions_dir: Path, num: int, title: str,
                       date: str = "2026-04-02 10:00:00",
                       decisions: list[str] | None = None,
                       open_questions: list[str] | None = None,
                       files_changed: list[str] | None = None,
                       summary: str = "Test summary."):
    """Create a minimal session markdown file for testing."""
    decisions = decisions or []
    open_questions = open_questions or []
    files_changed = files_changed or []

    lines = [
        f"# {title}",
        "",
        f"**Session**: abc{num:05d} | **Date**: {date} | **Branch**: main | **Turns**: {num * 10}",
        f"**Project**: /home/test/project",
        "",
        "## Summary",
        "",
        summary,
        "",
    ]

    if decisions:
        lines.append("## Key decisions")
        lines.append("")
        for d in decisions:
            lines.append(f"### {d}")
            lines.append(f"**Rationale**: because reasons")
            lines.append("")

    if open_questions:
        lines.append("## Open questions")
        lines.append("")
        for q in open_questions:
            lines.append(f"- {q}")
        lines.append("")

    if files_changed:
        lines.append("## Files changed")
        lines.append("")
        for f in files_changed:
            lines.append(f"- `{f}`")
        lines.append("")

    # Filename must sort chronologically
    fname = f"2026-04-0{num}_1000_abc{num:05d}_{title.lower().replace(' ', '-')[:30]}.md"
    (sessions_dir / fname).write_text("\n".join(lines))


class TestLoadSessions:
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            result = _load_sessions(Path(tmpdir))
            assert result == []

    def test_no_sessions_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _load_sessions(Path(tmpdir))
            assert result == []

    def test_loads_and_numbers_chronologically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "First session", "2026-04-01 10:00:00")
            _make_session_file(sessions_dir, 2, "Second session", "2026-04-02 10:00:00")
            _make_session_file(sessions_dir, 3, "Third session", "2026-04-03 10:00:00")

            result = _load_sessions(Path(tmpdir))
            assert len(result) == 3
            assert result[0]["number"] == 1
            assert result[0]["title"] == "First session"
            assert result[2]["number"] == 3
            assert result[2]["title"] == "Third session"

    def test_extracts_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(
                sessions_dir, 1, "Test metadata",
                date="2026-04-01 09:30:00",
                decisions=["Use postgres", "Skip Redis"],
                open_questions=["Should we add caching?"],
                files_changed=["src/db.py", "config.yaml"],
                summary="Migrated database layer.",
            )

            result = _load_sessions(Path(tmpdir))
            assert len(result) == 1
            s = result[0]
            assert s["date"] == "2026-04-01 09:30:00"
            assert s["turns"] == 10
            assert s["n_decisions"] == 2
            assert s["decisions"] == ["Use postgres", "Skip Redis"]
            assert s["open_questions"] == ["Should we add caching?"]
            assert s["files_changed"] == ["src/db.py", "config.yaml"]
            assert "Migrated" in s["summary"]


class TestShowSessionList:
    def test_output_format(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Alpha session", decisions=["Dec A"])
            _make_session_file(sessions_dir, 2, "Beta session", decisions=["Dec B", "Dec C"])

            sessions = _load_sessions(Path(tmpdir))
            # Rename tmpdir to simulate a project slug
            project_dir = Path(tmpdir)
            show_session_list(sessions, project_dir)

            output = capsys.readouterr().out
            assert "Alpha session" in output
            assert "Beta session" in output
            assert "chronicle rewind <N>" in output

    def test_arrow_marks_latest(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Old")
            _make_session_file(sessions_dir, 2, "New")

            sessions = _load_sessions(Path(tmpdir))
            show_session_list(sessions, Path(tmpdir))

            output = capsys.readouterr().out
            lines = output.strip().split("\n")
            # The arrow should be on the last session line
            session_lines = [l for l in lines if "Old" in l or "New" in l]
            assert not session_lines[0].strip().startswith("→")
            assert "→" in session_lines[1]


class TestShowSession:
    def test_shows_full_details(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(
                sessions_dir, 1, "Detailed session",
                decisions=["Big decision"],
                open_questions=["Remaining question"],
                files_changed=["main.py"],
                summary="Detailed work done.",
            )

            sessions = _load_sessions(Path(tmpdir))
            show_session(sessions[0])

            output = capsys.readouterr().out
            assert "Detailed session" in output
            assert "Big decision" in output
            assert "Remaining question" in output
            assert "main.py" in output
            assert "Detailed work done." in output


class TestShowSince:
    def test_shows_range(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "First", decisions=["Dec 1"])
            _make_session_file(sessions_dir, 2, "Second", decisions=["Dec 2"])
            _make_session_file(sessions_dir, 3, "Third", decisions=["Dec 3"])

            sessions = _load_sessions(Path(tmpdir))
            show_since(sessions, 2)

            output = capsys.readouterr().out
            assert "First" not in output
            assert "Second" in output
            assert "Third" in output
            assert "Sessions #2" in output

    def test_since_beyond_range(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Only")

            sessions = _load_sessions(Path(tmpdir))
            show_since(sessions, 99)

            output = capsys.readouterr().out
            assert "No sessions" in output


class TestShowDiff:
    def test_first_session_is_all_new(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Genesis", decisions=["Start project"])

            sessions = _load_sessions(Path(tmpdir))
            show_diff(sessions, 1)

            output = capsys.readouterr().out
            assert "First session" in output

    def test_diff_shows_new_decisions(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Session A",
                               decisions=["Use postgres"],
                               open_questions=["Should we cache?"])
            _make_session_file(sessions_dir, 2, "Session B",
                               decisions=["Use postgres", "Add Redis"],
                               open_questions=["Redis cluster size?"])

            sessions = _load_sessions(Path(tmpdir))
            show_diff(sessions, 2)

            output = capsys.readouterr().out
            assert "+ Add Redis" in output
            assert "Use postgres" not in output.split("NEW decisions")[1].split("NEW files")[0]

    def test_diff_shows_resolved_questions(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Session A",
                               open_questions=["Should we cache?", "Which DB?"])
            _make_session_file(sessions_dir, 2, "Session B",
                               open_questions=["Which DB?"])

            sessions = _load_sessions(Path(tmpdir))
            show_diff(sessions, 2)

            output = capsys.readouterr().out
            assert "Should we cache?" in output
            assert "RESOLVED" in output

    def test_diff_nonexistent_session(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir) / "sessions"
            sessions_dir.mkdir()
            _make_session_file(sessions_dir, 1, "Only")

            sessions = _load_sessions(Path(tmpdir))
            show_diff(sessions, 99)

            output = capsys.readouterr().out
            assert "not found" in output


class TestFindProjectDir:
    def test_partial_match(self):
        from chronicle.rewind import _find_project_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = Path(tmpdir) / "-home-user-myproject"
            proj.mkdir()
            with patch("chronicle.rewind.PROJECTS_DIR", Path(tmpdir)):
                result = _find_project_dir("myproject")
                assert result == proj

    def test_no_match(self):
        from chronicle.rewind import _find_project_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("chronicle.rewind.PROJECTS_DIR", Path(tmpdir)):
                result = _find_project_dir("nonexistent")
                assert result is None

    def test_cwd_match(self):
        from chronicle.rewind import _find_project_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            proj = Path(tmpdir) / "-home-user-myproject"
            proj.mkdir()
            with patch("chronicle.rewind.PROJECTS_DIR", Path(tmpdir)):
                with patch("os.getcwd", return_value="/home/user/myproject"):
                    result = _find_project_dir(None)
                    assert result == proj
