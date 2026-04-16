"""Unit tests for chronicle.locks."""
from __future__ import annotations

import multiprocessing
import os
import time

import pytest


@pytest.fixture
def isolated_locks(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".chronicle").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    import importlib
    import chronicle.config
    import chronicle.locks
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.locks)
    chronicle.locks._reset_daemon_lock_for_tests()
    yield
    chronicle.locks._reset_daemon_lock_for_tests()
    importlib.reload(chronicle.config)
    importlib.reload(chronicle.locks)


def test_acquire_daemon_lock_once(isolated_locks):
    from chronicle import locks
    assert locks.acquire_daemon_lock() is True
    assert locks.daemon_lock_still_valid() is True


def test_daemon_is_running_after_acquire(isolated_locks):
    from chronicle import locks
    assert locks.acquire_daemon_lock() is True
    running, pid = locks.daemon_is_running()
    assert running is True
    assert pid == os.getpid()


def test_processing_lock_context(isolated_locks):
    from chronicle import locks
    assert locks.processing_lock_held() is False
    with locks.processing_lock(blocking=True) as acquired:
        assert acquired is True
        assert locks.processing_lock_held() is True
    assert locks.processing_lock_held() is False


def _hold_processing_lock(duration: float, ready_path: str):
    """Helper for cross-process test: hold the lock, signal ready."""
    import chronicle.locks as L
    with L.processing_lock(blocking=True):
        # Signal to parent that we have the lock
        open(ready_path, "w").close()
        time.sleep(duration)


def test_processing_lock_serializes_across_processes(isolated_locks, tmp_path):
    """A second process trying non-blocking acquire should see held=True
    while the first process holds it.
    """
    from chronicle import locks
    ready = tmp_path / "ready"
    # Spawn a subprocess that holds the lock for 1s
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_hold_processing_lock, args=(1.0, str(ready)))
    p.start()
    # Wait until the child signals it's holding
    for _ in range(50):
        if ready.exists():
            break
        time.sleep(0.05)
    assert ready.exists(), "child did not acquire lock in time"
    # While the child holds, we should see held=True
    assert locks.processing_lock_held() is True
    # Our own non-blocking acquire should fail
    with locks.processing_lock(blocking=False) as acquired:
        assert acquired is False
    p.join(timeout=3)
    # After child exits, lock is free
    assert locks.processing_lock_held() is False
