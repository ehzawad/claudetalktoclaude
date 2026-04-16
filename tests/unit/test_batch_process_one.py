"""Unit tests for batch._process_one progress-loop latency.

Proves:
- fast completion returns immediately (no 15s sleep burn)
- slow completion emits a progress heartbeat after PROGRESS_INTERVAL_SECONDS
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import pytest


@dataclass
class _FakeDigest:
    session_id: str = "abc12345-fake-fake-fake-fakefakefake"
    total_turns: int = 1
    user_prompts: list = field(default_factory=list)
    project_slug: str = "-tmp-demo"


@dataclass
class _FakeEntry:
    is_error: bool = False
    is_empty: bool = False
    decisions: list = field(default_factory=list)


@pytest.mark.asyncio
async def test_fast_completion_does_not_wait_interval(monkeypatch, capsys):
    """A 10ms summarization should return in well under the heartbeat
    interval — if the old sleep-first loop were in place, this would
    take at least PROGRESS_INTERVAL_SECONDS."""
    from chronicle import batch
    monkeypatch.setattr(batch, "PROGRESS_INTERVAL_SECONDS", 5)

    async def fake_summarize(digest):
        await asyncio.sleep(0.01)
        return _FakeEntry()

    monkeypatch.setattr(batch, "async_summarize_session", fake_summarize)
    sem = asyncio.Semaphore(1)
    t0 = time.monotonic()
    result = await batch._process_one(_FakeDigest(), sem)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"_process_one took {elapsed:.2f}s (should be <1s)"
    assert result is not None
    out = capsys.readouterr().out
    assert "still processing" not in out


@pytest.mark.asyncio
async def test_slow_completion_emits_heartbeat(monkeypatch, capsys):
    """A summarization slower than the heartbeat interval should print
    at least one 'still processing' line."""
    from chronicle import batch
    monkeypatch.setattr(batch, "PROGRESS_INTERVAL_SECONDS", 0.05)

    async def slow_summarize(digest):
        await asyncio.sleep(0.2)
        return _FakeEntry()

    monkeypatch.setattr(batch, "async_summarize_session", slow_summarize)
    sem = asyncio.Semaphore(1)
    await batch._process_one(_FakeDigest(), sem)
    out = capsys.readouterr().out
    assert "still processing" in out
