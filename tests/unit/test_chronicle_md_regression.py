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
