"""Verify that `chronicle process` threads config.max_retries into
write_chronicle — not the function default. Regression for the silent
config/behavior drift between daemon (respected config) and batch
(used the default 3 regardless).
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    import importlib
    for mod in ("chronicle.config", "chronicle.storage", "chronicle.batch"):
        importlib.reload(__import__(mod, fromlist=["_"]))
    yield fake_home


class _FakeDigest:
    def __init__(self):
        import uuid as u
        self.session_id = str(u.uuid4())
        self.project_slug = "-tmp-demo"
        self.start_time = "2026-04-17T00:00:00Z"
        self.end_time = "2026-04-17T00:01:00Z"
        self.total_turns = 1
        self.user_prompts = []


class _FakeEntry:
    is_error = False
    is_empty = False
    decisions: list = []
    total_cost_usd = 0.0
    # session_filename() needs these
    start_time = "2026-04-17T00:00:00Z"
    session_id = "abc12345-fake-fake-fake-fakefakefake"
    title = "fake"


@pytest.mark.asyncio
async def test_batch_passes_config_max_retries_to_write_chronicle(
    isolated, monkeypatch,
):
    """Set max_retries=7 in config.json; run async_batch_process against
    one fake digest; capture the max_retries write_chronicle receives.
    """
    from chronicle import batch, config, extractor

    # Write custom config
    config.save_default_config()
    custom = {
        "processing_mode": "foreground",
        "concurrency": 1,
        "model": "opus",
        "fallback_model": "sonnet",
        "poll_interval_seconds": 5,
        "quiet_minutes": 5,
        "scan_interval_minutes": 30,
        "max_retries": 7,
        "skip_projects": [],
    }
    import json as j
    config.CONFIG_FILE.write_text(j.dumps(custom))

    # Stub find_all_sessions → one jsonl; extract_session → fake digest;
    # async_summarize_session → success entry.
    digest = _FakeDigest()

    def fake_find(_filter=None):
        return [(digest.project_slug, __import__("pathlib").Path("/fake.jsonl"))]

    def fake_extract(path):
        return digest

    async def fake_summarize(d):
        return _FakeEntry()

    received: dict = {}

    def fake_write(entry, dig, max_retries: int = 3):
        received["max_retries"] = max_retries

    monkeypatch.setattr(batch, "find_all_sessions", fake_find)
    monkeypatch.setattr(batch, "extract_session", fake_extract)
    monkeypatch.setattr(batch, "async_summarize_session", fake_summarize)
    monkeypatch.setattr(batch, "write_chronicle", fake_write)
    # should_skip → process the session
    monkeypatch.setattr(batch, "should_skip", lambda *a, **kw: None)
    # Skip rebuild_prompts_section and project_chronicle_dir — they touch the fs
    monkeypatch.setattr(batch, "rebuild_prompts_section", lambda slug: None)

    await batch.async_batch_process(workers=1)

    assert received.get("max_retries") == 7, (
        f"batch ignored config max_retries (got {received})"
    )
