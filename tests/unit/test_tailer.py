"""Unit tests for the partial-line-safe event tailer in daemon._read_new_events."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def isolated_events(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    import importlib
    import chronicle.config
    import chronicle.daemon
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.daemon)
    yield fake_home / ".chronicle" / "events.jsonl"
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.daemon)


def _append(path, line: str):
    with open(path, "ab") as f:
        f.write(line.encode())


def test_reads_complete_lines(isolated_events):
    from chronicle import daemon
    _append(isolated_events, '{"a": 1}\n{"b": 2}\n')
    events, new_offset = daemon._read_new_events(0)
    assert len(events) == 2
    assert events[0]["a"] == 1
    assert events[1]["b"] == 2
    assert new_offset == isolated_events.stat().st_size


def test_holds_back_partial_final_line(isolated_events):
    """Partial line at EOF must NOT advance the offset past it."""
    from chronicle import daemon
    _append(isolated_events, '{"complete": 1}\n{"partial": ')
    events, new_offset = daemon._read_new_events(0)
    assert len(events) == 1
    # Offset should be at the start of the partial line (16 bytes in)
    assert new_offset == len('{"complete": 1}\n')
    # After the partial line completes, next read finishes it
    _append(isolated_events, '2}\n')
    events2, new_offset2 = daemon._read_new_events(new_offset)
    assert len(events2) == 1
    assert events2[0]["partial"] == 2
    assert new_offset2 == isolated_events.stat().st_size


def test_skips_malformed_but_advances(isolated_events):
    """Complete-but-invalid JSON lines advance the offset (won't reappear)."""
    from chronicle import daemon
    _append(isolated_events, 'not json\n{"good": 1}\n')
    events, new_offset = daemon._read_new_events(0)
    assert len(events) == 1
    assert events[0]["good"] == 1
    assert new_offset == isolated_events.stat().st_size


def test_offset_past_eof_resets_to_zero(isolated_events, capsys):
    from chronicle import daemon
    _append(isolated_events, '{"x": 1}\n')
    events, new_offset = daemon._read_new_events(999999)
    assert len(events) == 1  # Read from 0
    captured = capsys.readouterr()
    assert "resetting to 0" in captured.out


def test_empty_file_yields_no_events(isolated_events):
    from chronicle import daemon
    isolated_events.touch()
    events, new_offset = daemon._read_new_events(0)
    assert events == []
    assert new_offset == 0
