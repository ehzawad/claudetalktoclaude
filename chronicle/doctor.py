"""`chronicle doctor` — read-only diagnostic command.

Reports mode, resolved claude binary, daemon status, service drift,
marker counts, and any pending sessions. Used after install and when
debugging why sessions aren't being processed.

Never mutates state.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from . import service
from .claude_cli import try_resolve_claude_binary
from .config import (
    CHRONICLE_DIR, CLAUDE_PROJECTS, CONFIG_FILE, FAILED_DIR,
    PROCESSED_DIR, PROCESSING_LOCK,
)
from .locks import daemon_is_running, processing_lock_held
from .mode import get_processing_mode
from .storage import is_succeeded, is_terminal_failure, list_failed, session_hash


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * len(title))


def _count_pending_jsonls() -> tuple[int, int, int]:
    """Return (processed_ok, failed_term, pending) across all sessions."""
    if not CLAUDE_PROJECTS.exists():
        return 0, 0, 0
    ok = 0
    term = 0
    pending = 0
    for proj in CLAUDE_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for jsonl in proj.glob("*.jsonl"):
            if "subagents" in str(jsonl):
                continue
            sid = jsonl.stem
            if is_succeeded(sid):
                ok += 1
            elif is_terminal_failure(sid):
                term += 1
            else:
                pending += 1
    return ok, term, pending


def run() -> int:
    mode = get_processing_mode()
    _section("chronicle doctor")
    print(f"version:     {__import__('chronicle').__version__}")
    print(f"chronicle:   {shutil.which('chronicle') or '(not on PATH)'}")
    print(f"mode:        {mode}")
    print(f"config:      {CONFIG_FILE}")

    _section("claude binary")
    claude = try_resolve_claude_binary()
    if claude:
        print(f"resolved:    {claude}")
    else:
        print("resolved:    NOT FOUND — chronicle cannot summarize", file=sys.stderr)
    print(f"PATH:        {os.environ.get('PATH', '(empty)')}")

    _section("daemon")
    running, pid = daemon_is_running()
    if running:
        print(f"running:     yes (pid {pid})")
    else:
        print("running:     no")
    svc_path = service.service_file_path()
    svc_installed = service.service_installed()
    svc_running = service.service_running()
    print(f"service file: {svc_path} "
          f"({'installed' if svc_installed else 'absent'})")
    print(f"service status: "
          f"{'active' if svc_running else 'inactive'}")

    _section("drift")
    warnings = service.mode_drift_warnings()
    if not warnings:
        print("(no drift detected)")
    else:
        for w in warnings:
            print(f"!  {w}")

    _section("locks")
    print(f"daemon pid file: {CHRONICLE_DIR / 'daemon.pid'}")
    print(f"processing lock: {PROCESSING_LOCK} "
          f"({'HELD' if processing_lock_held() else 'free'})")

    _section("sessions")
    ok, term, pending = _count_pending_jsonls()
    processed_markers = len(list(PROCESSED_DIR.glob("*"))) if PROCESSED_DIR.exists() else 0
    failed_records = list_failed()
    failed_terminal = sum(1 for r in failed_records if r.get("terminal"))
    failed_retryable = len(failed_records) - failed_terminal
    print(f"source JSONLs (under ~/.claude/projects/): ")
    print(f"  processed OK:      {ok}")
    print(f"  terminal failure:  {term}")
    print(f"  unprocessed:       {pending}")
    print(f"markers:")
    print(f"  .processed/        {processed_markers} entries")
    print(f"  .failed/           {len(failed_records)} entries "
          f"({failed_terminal} terminal, {failed_retryable} retryable)")

    if failed_terminal:
        _section("terminal failures (first 5)")
        for rec in failed_records[:5]:
            if not rec.get("terminal"):
                continue
            print(f"  {rec.get('session_id', '?')[:8]}  "
                  f"attempts={rec.get('attempts', 0)}  "
                  f"kind={rec.get('last_error_kind', '?')}")
            msg = rec.get("last_error_message", "")
            if msg:
                print(f"    {msg[:140]}")
        if failed_terminal > 5:
            print(f"  ... and {failed_terminal - 5} more")
        print()
        print("To retry all terminal failures after fixing the root cause:")
        print("  chronicle process --retry-failed --workers 5")

    print()
    return 0 if not warnings else 1
