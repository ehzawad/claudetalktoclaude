"""End-to-end CLI suite: drive representative chronicle commands via
subprocess and assert they work as a user would see them.

Fully hermetic: isolated HOME, a fake `claude` on PATH (no real tokens), and
fake `launchctl`/`systemctl` for the daemon-mode round-trip (no real service
manager). Run via `python -m chronicle ...` so argv parsing, exit codes, and
on-disk side-effects are all exercised end to end.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FAKE_CLAUDE = Path(__file__).resolve().parent.parent / "fixtures" / "fake_claude.py"


def _install_fake_claude(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    dst = bin_dir / "claude"
    dst.write_text(FAKE_CLAUDE.read_text())
    dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _install_fake_service_managers(bin_dir: Path) -> None:
    """Fake launchctl + systemctl that always succeed and log their argv, so
    install-daemon/uninstall-daemon never touch the real service manager."""
    log = bin_dir / "svc.log"
    for name in ("launchctl", "systemctl"):
        p = bin_dir / name
        p.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            f"open({str(log)!r}, 'a').write(' '.join(sys.argv) + chr(10))\n"
            "sys.exit(0)\n"
        )
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _seed_jsonl(claude_projects: Path, slug: str, sid: str, prompt: str = "please help refactor") -> Path:
    proj = claude_projects / slug
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{sid}.jsonl"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        json.dumps({"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
                    "timestamp": now, "cwd": "/tmp/x", "gitBranch": "main",
                    "message": {"content": prompt}}),
        json.dumps({"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
                    "timestamp": now, "message": {"content": [{"type": "text", "text": "on it"}]}}),
    ]
    jsonl.write_text("\n".join(lines) + "\n")
    return jsonl


def _run(args, *, home, bin_dir, fake_mode="success", extra_env=None, cwd=None, timeout=60):
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin"
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["FAKE_CLAUDE_MODE"] = fake_mode
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CHRONICLE_HOME"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "chronicle"] + list(args)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:16]


def _make_installed_footprint(home: Path) -> Path:
    """Minimal on-disk install footprint so uninstall/install-daemon see a real
    install in the isolated HOME (executables, runtime dir, settings hook)."""
    localbin = home / ".local" / "bin"
    localbin.mkdir(parents=True, exist_ok=True)
    chron = localbin / "chronicle"
    chron.write_text("#!/bin/sh\necho 'chronicle 0.8.9'\n")
    chron.chmod(0o755)
    hook = localbin / "chronicle-hook"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    (home / ".chronicle" / "runtime").mkdir(parents=True, exist_ok=True)
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook"}]}]}}))
    return chron


@pytest.fixture
def cli(tmp_path):
    home = tmp_path / "home"
    (home / ".chronicle").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    _install_fake_claude(bin_dir)
    _install_fake_service_managers(bin_dir)
    return home, bin_dir, tmp_path


# ---------------- top-level ----------------

class TestTopLevel:
    def test_version(self, cli):
        home, bin_dir, _ = cli
        from chronicle import __version__
        for flag in ("--version", "-V"):
            r = _run([flag], home=home, bin_dir=bin_dir)
            assert r.returncode == 0
            assert r.stdout.strip() == f"chronicle {__version__}"

    @pytest.mark.parametrize("args", [["--help"], ["-h"], ["help"], []])
    def test_help_and_no_args(self, cli, args):
        home, bin_dir, _ = cli
        r = _run(args, home=home, bin_dir=bin_dir)
        assert r.returncode == 0, r.stderr
        assert "Unknown command" not in r.stdout
        assert "chronicle" in r.stdout.lower()

    def test_unknown_command(self, cli):
        home, bin_dir, _ = cli
        r = _run(["bogus-cmd"], home=home, bin_dir=bin_dir)
        assert r.returncode == 1
        assert "Unknown command" in r.stdout or "Unknown command" in r.stderr


# ---------------- doctor ----------------

class TestDoctor:
    def test_doctor_text(self, cli):
        home, bin_dir, _ = cli
        r = _run(["doctor"], home=home, bin_dir=bin_dir)
        assert r.returncode in (0, 1)  # 0 if clean; 1 only on drift/unresolved
        assert "claude" in r.stdout.lower()

    def test_doctor_json_valid(self, cli):
        home, bin_dir, _ = cli
        r = _run(["doctor", "--json"], home=home, bin_dir=bin_dir)
        doc = json.loads(r.stdout)
        assert doc["schema_version"] == 1
        assert "ok" in doc and isinstance(doc["ok"], bool)
        assert doc["mode"] == "foreground"

    def test_doctor_json_reflects_processed(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-doc", sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        doc = json.loads(_run(["doctor", "--json"], home=home, bin_dir=bin_dir).stdout)
        assert doc["sessions"]["processed_ok"] >= 1


# ---------------- process ----------------

class TestProcess:
    def test_process_real_run(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-proc", sid)
        r = _run(["process", "--workers", "2"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0, r.stderr + r.stdout
        sessions = home / ".chronicle" / "projects" / "tmp-proc" / "sessions"
        assert list(sessions.glob(f"*_{sid[:8]}*.md"))
        assert (home / ".chronicle" / ".processed" / _hash(sid)).exists()

    def test_process_dry_run_writes_nothing(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-dry", sid)
        r = _run(["process", "--dry-run"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0
        assert "DRY RUN" in r.stdout
        assert not (home / ".chronicle" / ".processed" / _hash(sid)).exists()
        assert not (home / ".chronicle" / "projects" / "tmp-dry" / "chronicle.md").exists()

    def test_process_force_reprocesses(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-force", sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        assert (home / ".chronicle" / ".processed" / _hash(sid)).exists()
        r = _run(["process", "--force", "--workers", "1"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0
        assert "Processing 1 session" in r.stdout

    def test_process_project_filter(self, cli):
        home, bin_dir, _ = cli
        sid_alpha = str(uuid.uuid4())
        sid_beta = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-alpha", sid_alpha)
        _seed_jsonl(home / ".claude" / "projects", "-tmp-beta", sid_beta)
        r = _run(["process", "--project", "alpha", "--dry-run"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0
        # --project alpha must select only the alpha session, not beta. Assert on
        # the stable session id rather than the display name (which is now the
        # cwd basename, not the raw slug).
        assert sid_alpha[:8] in r.stdout and sid_beta[:8] not in r.stdout

    def test_process_workers_zero_does_not_hang(self, cli):
        home, bin_dir, _ = cli
        _seed_jsonl(home / ".claude" / "projects", "-tmp-w0", str(uuid.uuid4()))
        r = _run(["process", "--workers", "0"], home=home, bin_dir=bin_dir, timeout=30)
        assert r.returncode == 0  # floored to 1, completes

    def test_batch_alias(self, cli):
        home, bin_dir, _ = cli
        _seed_jsonl(home / ".claude" / "projects", "-tmp-batch", str(uuid.uuid4()))
        r = _run(["batch", "--dry-run"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0 and "DRY RUN" in r.stdout


# ---------------- query ----------------

class TestQuery:
    def test_query_projects_after_process(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-q", sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        r = _run(["query", "projects"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0
        # The project list shows the de-dashed slug (cross-project disambiguation).
        assert "tmp-q" in r.stdout and "Total" in r.stdout

    def test_query_projects_canonical_id_divergent(self, cli):
        # BUG-20: filename stem != in-file sessionId; query projects must count
        # it processed (canonical id), agreeing with doctor.
        home, bin_dir, _ = cli
        slug = "-tmp-div"
        proj = home / ".claude" / "projects" / slug
        proj.mkdir(parents=True)
        (proj / "filename-stem.jsonl").write_text(
            json.dumps({"type": "user", "sessionId": "internal-xyz",
                        "message": {"content": "hi"}}) + "\n")
        # mark the in-file id succeeded directly
        _run(["process", "--dry-run"], home=home, bin_dir=bin_dir)  # ensure dirs
        import importlib
        env = os.environ.copy()
        # mark via a tiny in-process call using the isolated HOME
        sub = subprocess.run(
            [sys.executable, "-c",
             "import os;os.environ['HOME']=%r;"
             "import importlib,chronicle.config,chronicle.storage as s;"
             "importlib.reload(chronicle.config);importlib.reload(s);"
             "s.mark_succeeded('internal-xyz','2026-05-29T10:00:00Z',0.0)" % str(home)],
            capture_output=True, text=True)
        assert sub.returncode == 0, sub.stderr
        r = _run(["query", "projects"], home=home, bin_dir=bin_dir)
        # the slug row should show OK 1, not Pend 1 (list shows de-dashed slug)
        row = [l for l in r.stdout.splitlines() if slug.lstrip("-") in l]
        assert row and " 1 " in row[0].replace("  ", " "), f"expected OK=1: {row}"

    def test_query_timeline_and_search(self, cli):
        home, bin_dir, _ = cli
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", "-tmp-ts", sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        rt = _run(["query", "timeline"], home=home, bin_dir=bin_dir)
        assert rt.returncode == 0
        rs = _run(["query", "search", "stubbed"], home=home, bin_dir=bin_dir)
        assert rs.returncode == 0  # the fake summary contains "stubbed"

    def test_query_sessions_cwd(self, cli):
        home, bin_dir, tmp = cli
        from chronicle.config import project_slug_for
        work = tmp / "work"
        work.mkdir()
        slug = project_slug_for(str(work))
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", slug, sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        r = _run(["query", "sessions"], home=home, bin_dir=bin_dir, cwd=str(work))
        assert r.returncode == 0


# ---------------- rewind ----------------

class TestRewind:
    def _seed_processed(self, home, bin_dir, tmp):
        from chronicle.config import project_slug_for
        work = tmp / "rw"
        work.mkdir()
        slug = project_slug_for(str(work))
        sid = str(uuid.uuid4())
        _seed_jsonl(home / ".claude" / "projects", slug, sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        return work

    def test_rewind_list_and_view(self, cli):
        home, bin_dir, tmp = cli
        work = self._seed_processed(home, bin_dir, tmp)
        rl = _run(["rewind"], home=home, bin_dir=bin_dir, cwd=str(work))
        assert rl.returncode == 0
        rv = _run(["rewind", "1"], home=home, bin_dir=bin_dir, cwd=str(work))
        assert rv.returncode == 0

    def test_rewind_since_and_diff(self, cli):
        home, bin_dir, tmp = cli
        work = self._seed_processed(home, bin_dir, tmp)
        assert _run(["rewind", "--since", "1"], home=home, bin_dir=bin_dir, cwd=str(work)).returncode == 0
        assert _run(["rewind", "--diff", "1"], home=home, bin_dir=bin_dir, cwd=str(work)).returncode == 0

    def test_rewind_prune_eof_declines(self, cli):
        home, bin_dir, tmp = cli
        work = self._seed_processed(home, bin_dir, tmp)
        # closed stdin -> BUG-18 graceful decline, not a traceback
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        r = subprocess.run([sys.executable, "-m", "chronicle", "rewind", "--prune"],
                           env=env, cwd=str(work), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
        assert "Traceback" not in (r.stdout + r.stderr)

    def test_rewind_summary(self, cli):
        home, bin_dir, tmp = cli
        work = self._seed_processed(home, bin_dir, tmp)
        r = _run(["rewind", "--summary", "1"], home=home, bin_dir=bin_dir,
                 fake_mode="result", extra_env={"FAKE_CLAUDE_RESULT": "STUB SUMMARY TEXT"},
                 cwd=str(work))
        assert r.returncode == 0
        assert "STUB SUMMARY TEXT" in r.stdout


# ---------------- insight / story ----------------

class TestInsightStory:
    def _processed_slug(self, home, bin_dir):
        sid = str(uuid.uuid4())
        slug = "-tmp-is"
        _seed_jsonl(home / ".claude" / "projects", slug, sid)
        _run(["process", "--workers", "1"], home=home, bin_dir=bin_dir)
        return slug

    def test_insight_writes_html(self, cli):
        home, bin_dir, _ = cli
        slug = self._processed_slug(home, bin_dir)
        r = _run(["insight", slug.lstrip("-")], home=home, bin_dir=bin_dir, fake_mode="result",
                 extra_env={"FAKE_CLAUDE_RESULT": "<!DOCTYPE html><html><body>stub</body></html>"})
        assert r.returncode == 0, r.stderr + r.stdout
        from chronicle.config import storage_key
        assert (home / ".chronicle" / "projects" / storage_key(slug) / "insight.html").exists()

    def test_story_writes_md(self, cli):
        home, bin_dir, _ = cli
        slug = self._processed_slug(home, bin_dir)
        r = _run(["story", slug.lstrip("-")], home=home, bin_dir=bin_dir, fake_mode="result",
                 extra_env={"FAKE_CLAUDE_RESULT": "# Story\n\nstub narrative"})
        assert r.returncode == 0, r.stderr + r.stdout
        from chronicle.config import storage_key
        assert (home / ".chronicle" / "projects" / storage_key(slug) / "story.md").exists()


# ---------------- install-hooks ----------------

class TestInstallHooks:
    def test_idempotent_and_preserves_unrelated(self, cli):
        home, bin_dir, _ = cli
        settings = home / ".claude" / "settings.json"
        settings.write_text(json.dumps({
            "hooks": {"SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": "echo other"}]}]}}))
        for _ in range(2):
            r = _run(["install-hooks", str(settings)], home=home, bin_dir=bin_dir)
            assert r.returncode == 0, r.stderr
        data = json.loads(settings.read_text())
        cmds = [hk.get("command", "") for ev in data["hooks"].values()
                for g in ev for hk in g.get("hooks", [])]
        assert sum("chronicle-hook" in c for c in cmds) == 4  # one per event, no dupes
        assert any("echo other" in c for c in cmds)  # unrelated hook preserved


# ---------------- uninstall --dry-run ----------------

class TestUninstallDryRun:
    def test_dry_run_deletes_nothing(self, cli):
        home, bin_dir, _ = cli
        chron = _make_installed_footprint(home)
        ef = home / ".chronicle" / "events.jsonl"
        ef.write_text("{}\n")
        r = _run(["uninstall", "--dry-run"], home=home, bin_dir=bin_dir)
        assert r.returncode == 0
        assert "DRY RUN" in r.stdout
        assert ef.exists() and chron.exists()  # nothing deleted on dry-run


# ---------------- daemon control ----------------

class TestDaemonControl:
    def test_status_and_stop_no_daemon(self, cli):
        home, bin_dir, _ = cli
        rs = _run(["daemon", "--status"], home=home, bin_dir=bin_dir)
        assert rs.returncode == 0 and "not running" in rs.stdout.lower()
        rt = _run(["daemon", "--stop"], home=home, bin_dir=bin_dir)
        assert rt.returncode == 0 and "no daemon" in rt.stdout.lower()


# ---------------- install-daemon <-> uninstall-daemon (hermetic) ----------------

class TestDaemonModeRoundTrip:
    def test_install_then_uninstall_daemon(self, cli):
        home, bin_dir, _ = cli
        _make_installed_footprint(home)  # install-daemon needs ~/.local/bin/chronicle
        config = home / ".chronicle" / "config.json"
        ri = _run(["install-daemon"], home=home, bin_dir=bin_dir, timeout=30)
        assert ri.returncode == 0, ri.stderr + ri.stdout
        assert json.loads(config.read_text())["processing_mode"] == "background"
        ru = _run(["uninstall-daemon"], home=home, bin_dir=bin_dir, timeout=30)
        assert ru.returncode == 0, ru.stderr + ru.stdout
        assert json.loads(config.read_text())["processing_mode"] == "foreground"
