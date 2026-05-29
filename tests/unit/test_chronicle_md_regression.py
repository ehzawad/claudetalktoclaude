"""Regression: chronicle.md must keep every session's detail body (multi-session
+ reprocess) — guards against the prompts-block-truncates-detail data loss."""
from __future__ import annotations
import importlib
import pytest


@pytest.fixture
def isolated_chronicle(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    import chronicle.config, chronicle.summarizer, chronicle.storage
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.summarizer)
    importlib.reload(chronicle.storage)
    yield fake_home / ".chronicle"
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.summarizer)
    importlib.reload(chronicle.storage)


def _digest(sid, slug):
    class D:
        session_id = sid
        project_slug = slug
        end_time = "2026-05-29T10:00:00Z"
    return D()


def _entry(sid, n, slug, suffix=""):
    from chronicle.summarizer import ChronicleEntry
    from chronicle.extractor import UserPrompt
    return ChronicleEntry(
        session_id=sid, project_path="/tmp/p", project_slug=slug,
        start_time=f"2026-05-29T1{n}:00:00Z", end_time=f"2026-05-29T1{n}:30:00Z",
        git_branch="main",
        user_prompts=[UserPrompt(text=f"p{n}", timestamp=f"2026-05-29T1{n}:00:00Z", uuid=f"u{n}")],
        title=f"S{n}", summary=f"Sum{n}.", narrative=f"NARRATIVE_{n}{suffix}",
        decisions=[{"what": f"D{n}", "why": f"B{n}", "status": "done"}],
        total_turns=5, total_cost_usd=0.01,
    )


def test_multi_session_keeps_all_detail_bodies(isolated_chronicle):
    from chronicle import storage
    from chronicle.config import project_chronicle_dir
    slug = "-tmp-proj"
    for n in (1, 2, 3):
        sid = f"sess-{n}-aaaaaaaa"
        storage.write_chronicle(_entry(sid, n, slug), _digest(sid, slug), max_retries=3)
    content = (project_chronicle_dir(slug) / "chronicle.md").read_text()
    assert content.count("<!-- session:") == 3
    assert content.count("<!-- prompts -->") == 1
    for n in (1, 2, 3):
        assert f"NARRATIVE_{n}" in content


def test_reprocess_preserves_other_sessions_and_no_orphans(isolated_chronicle):
    from chronicle import storage
    from chronicle.config import project_chronicle_dir
    slug = "-tmp-proj"
    for n in (1, 2, 3):
        sid = f"sess-{n}-aaaaaaaa"
        storage.write_chronicle(_entry(sid, n, slug), _digest(sid, slug), max_retries=3)
    # Reprocess the middle session with new narrative.
    storage.write_chronicle(_entry("sess-2-aaaaaaaa", 2, slug, suffix="_REPROC"),
                            _digest("sess-2-aaaaaaaa", slug), max_retries=3)
    content = (project_chronicle_dir(slug) / "chronicle.md").read_text()
    assert content.count("<!-- session:") == 3
    assert content.count("<!-- prompts -->") == 1
    # No orphaned prompt <details> blocks: one per session.
    assert content.count("<details><summary>User prompts (verbatim)</summary>") == 3
    assert "NARRATIVE_2_REPROC" in content
    assert "NARRATIVE_1" in content and "NARRATIVE_3" in content


def test_corrupt_chronicle_missing_separator_self_heals(isolated_chronicle):
    # BUG-09: a chronicle.md with the end marker but a missing timeline
    # separator must NOT raise (which escapes write_chronicle with no marker ->
    # infinite re-summarization / batch abort); append must repair in place.
    from chronicle import storage
    from chronicle.config import project_chronicle_dir
    slug = "-tmp-proj9"
    storage.write_chronicle(_entry("sess-1-aaaaaaaa", 1, slug),
                            _digest("sess-1-aaaaaaaa", slug), max_retries=3)
    cf = project_chronicle_dir(slug) / "chronicle.md"
    content = cf.read_text()
    assert storage._TIMELINE_SEP + "\n" in content
    # Corrupt: delete the separator line, keep the end marker.
    cf.write_text(content.replace(storage._TIMELINE_SEP + "\n", ""))
    assert storage._TIMELINE_END in cf.read_text()
    assert storage._TIMELINE_SEP not in cf.read_text()
    # Must self-heal, not raise (fails on current code with ValueError).
    storage.write_chronicle(_entry("sess-2-aaaaaaaa", 2, slug),
                            _digest("sess-2-aaaaaaaa", slug), max_retries=3)
    repaired = cf.read_text()
    assert repaired.count(storage._TIMELINE_HEADER) == 1
    assert repaired.count(storage._TIMELINE_SEP) == 1
    assert repaired.count(storage._TIMELINE_END) == 1
    assert "NARRATIVE_2" in repaired


def test_long_title_reprocess_no_orphan_heading(isolated_chronicle):
    # BUG-21: _remove_session_entry's old 300-char backward window orphaned the
    # heading for titles longer than ~295 chars on reprocess.
    from chronicle import storage
    from chronicle.config import project_chronicle_dir
    from chronicle.summarizer import ChronicleEntry
    from chronicle.extractor import UserPrompt
    slug = "-tmp-proj21"
    long_title = "X" * 350

    def mk(suffix):
        return ChronicleEntry(
            session_id="sess-long-aaaaaaaa", project_path="/tmp/p", project_slug=slug,
            start_time="2026-05-29T11:00:00Z", end_time="2026-05-29T11:30:00Z",
            git_branch="main",
            user_prompts=[UserPrompt(text="p", timestamp="2026-05-29T11:00:00Z", uuid="u")],
            title=long_title, summary="S.", narrative=f"NARR{suffix}",
            decisions=[{"what": "D", "why": "B", "status": "done"}],
            total_turns=1, total_cost_usd=0.01,
        )

    d = _digest("sess-long-aaaaaaaa", slug)
    storage.write_chronicle(mk(""), d, max_retries=3)
    storage.write_chronicle(mk("_REPROC"), d, max_retries=3)  # reprocess -> _remove_session_entry
    content = (project_chronicle_dir(slug) / "chronicle.md").read_text()
    # Count exact heading LINES (the prompts section repeats the title as a
    # "### <title>" subheading, which contains "## <title>" as a substring).
    assert content.split("\n").count("## " + long_title) == 1, "orphaned long-title heading line"
    assert content.count("<!-- session:") == 1
    assert "NARR_REPROC" in content
