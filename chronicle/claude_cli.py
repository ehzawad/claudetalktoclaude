"""Central Claude CLI subprocess management.

Solves three cross-cutting concerns:

1. PATH resolution — finds the `claude` binary even when the daemon runs
   under launchd's minimal PATH. Uses shutil.which + fallback directories.
2. Subprocess env — strips auth/endpoint env vars so subscription routing
   always wins over API-key or proxy-gateway routing
   (anthropics/claude-code#2051).
3. Error classification — distinguishes INFRA (missing binary, perm
   denied, auth failure) from TRANSIENT (timeout, claude's own is_error)
   from PARSE (bad JSON). Retry accounting only penalizes non-INFRA.

Also maintains a registry of in-flight subprocesses so daemon shutdown
can terminate them instead of orphaning them.

Why we pass an absolute path: Python's subprocess lookup for the
executable when `env=` is supplied is implementation-dependent across
POSIX platforms — some paths use the parent's PATH, some use env[PATH].
Passing an already-resolved absolute path sidesteps the ambiguity and
guarantees the daemon finds `claude` regardless of who spawned it
(launchd, systemd, hook, or interactive shell).
"""

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional


# Env vars that route away from the Claude.ai subscription.
# Stripped before every claude -p invocation.
_STRIP_ENV_VARS = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
})


def _fallback_bin_dirs() -> list[Path]:
    """Dirs to search when shutil.which("claude") misses.

    Computed per-call so HOME changes (e.g. test fixtures) are honored.
    """
    return [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]


def _standard_path_dirs() -> list[Path]:
    """Dirs prepended to the subprocess PATH so nested invocations work
    under launchd's minimal env."""
    return [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]


class ErrorKind(Enum):
    """Classification of subprocess failures."""
    INFRA = "infra"          # Binary missing, perm denied, auth — don't charge retries
    TRANSIENT = "transient"  # Timeout, claude's is_error — count against retries
    PARSE = "parse"          # Output unparseable — count against retries


class ClaudeNotFound(RuntimeError):
    """Raised when the claude binary cannot be resolved."""


@dataclass
class ClaudeResult:
    """Outcome of a `claude -p` invocation.

    If error_kind is None the call succeeded and stdout_json holds the
    parsed outer JSON. Otherwise error_message describes the failure.
    """
    stdout_json: Optional[dict] = None
    total_cost_usd: float = 0.0
    error_kind: Optional[ErrorKind] = None
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.error_kind is None


# ---------- Binary resolution ----------

_cached_claude_path: Optional[Path] = None


def resolve_claude_binary(force_refresh: bool = False) -> Path:
    """Return the absolute path to `claude`. Raises ClaudeNotFound if absent.

    Searches os.environ["PATH"] (via shutil.which), then curated fallback
    dirs. Caches the hit for the process lifetime.
    """
    global _cached_claude_path
    if _cached_claude_path is not None and not force_refresh:
        if _cached_claude_path.exists():
            return _cached_claude_path
        _cached_claude_path = None

    hit = shutil.which("claude")
    if hit:
        _cached_claude_path = Path(hit).resolve()
        return _cached_claude_path

    for d in _fallback_bin_dirs():
        candidate = d / "claude"
        if candidate.exists() and os.access(candidate, os.X_OK):
            _cached_claude_path = candidate.resolve()
            return _cached_claude_path

    searched = [os.environ.get("PATH", "")] + [str(d) for d in _fallback_bin_dirs()]
    raise ClaudeNotFound(
        "Could not find `claude` binary. Searched: "
        + " | ".join(searched)
        + ". Install Claude Code CLI or ensure it is on the daemon's PATH "
        "(see `chronicle doctor`)."
    )


def try_resolve_claude_binary() -> Optional[Path]:
    """Like resolve_claude_binary but returns None on miss instead of raising."""
    try:
        return resolve_claude_binary()
    except ClaudeNotFound:
        return None


def build_subprocess_env(base: Optional[dict] = None) -> dict:
    """Env dict suitable for spawning `claude -p`.

    - Strips routing-conflicting vars (API key, auth token, base URL).
    - Prepends standard bin dirs to PATH so subprocess nested under claude
      also works under launchd/systemd minimal env.
    """
    src = base if base is not None else os.environ
    env = {k: v for k, v in src.items() if k not in _STRIP_ENV_VARS}
    existing = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    extra = [str(d) for d in _standard_path_dirs() if d.exists()]
    seen: set[str] = set()
    merged: list[str] = []
    for p in extra + existing:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    env["PATH"] = os.pathsep.join(merged)
    return env


# ---------- Subprocess registry (graceful shutdown) ----------

_active_procs: "set[asyncio.subprocess.Process]" = set()


def _register(proc: "asyncio.subprocess.Process") -> None:
    _active_procs.add(proc)


def _unregister(proc: "asyncio.subprocess.Process") -> None:
    _active_procs.discard(proc)


async def terminate_active_subprocesses(grace_seconds: float = 5.0) -> dict:
    """Terminate all in-flight claude subprocesses. Called on daemon shutdown.

    Sends SIGTERM, waits up to grace_seconds, then SIGKILL stragglers.
    Always reaps via wait() after kill to avoid zombies.
    Returns {"terminated": N, "killed": M} for logging.
    """
    if not _active_procs:
        return {"terminated": 0, "killed": 0}
    victims = list(_active_procs)
    for p in victims:
        try:
            p.terminate()
        except ProcessLookupError:
            pass
    await asyncio.sleep(grace_seconds)
    killed = 0
    for p in victims:
        if p.returncode is None:
            try:
                p.kill()
                killed += 1
            except ProcessLookupError:
                pass
    # Reap all victims so we don't leave zombies.
    for p in victims:
        try:
            await asyncio.wait_for(p.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
    return {"terminated": len(victims), "killed": killed}


def active_subprocess_count() -> int:
    """For diagnostics / tests."""
    return len(_active_procs)


# ---------- Core invocation ----------

async def spawn_claude(
    prompt: str,
    *,
    model: str,
    fallback_model: str,
    effort: str = "max",
    json_schema: Optional[dict] = None,
    extra_flags: Iterable[str] = (),
    timeout: float = 300.0,
) -> ClaudeResult:
    """Invoke `claude -p` and return a classified result.

    Never raises for expected failure paths; returns a ClaudeResult with
    error_kind set. The caller decides whether that counts against a
    retry budget (see ErrorKind).
    """
    try:
        claude_bin = resolve_claude_binary()
    except ClaudeNotFound as e:
        return ClaudeResult(error_kind=ErrorKind.INFRA, error_message=str(e))

    args = [
        str(claude_bin), "-p",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
        "--effort", effort,
        "--fallback-model", fallback_model,
    ]
    if json_schema is not None:
        args += ["--json-schema", json.dumps(json_schema)]
    args += list(extra_flags)

    env = build_subprocess_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as e:
        # The binary existed at resolve time but vanished between resolve
        # and spawn. Rare but possible during uninstall.
        return ClaudeResult(
            error_kind=ErrorKind.INFRA,
            error_message=f"claude binary vanished before spawn: {e}",
        )
    except PermissionError as e:
        return ClaudeResult(
            error_kind=ErrorKind.INFRA,
            error_message=f"permission denied spawning claude: {e}",
        )

    _register(proc)
    try:
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
            return ClaudeResult(
                error_kind=ErrorKind.TRANSIENT,
                error_message=f"claude -p timed out after {timeout}s",
            )
    finally:
        _unregister(proc)

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")

    if proc.returncode != 0:
        msg = stderr[:300] or stdout[:300] or f"exit {proc.returncode}"
        combined = (stderr + " " + stdout).lower()
        infra_hints = (
            "command not found", "no such file",
            "not authenticated", "authentication required",
            "unauthorized", "please run", "please log in",
        )
        if any(h in combined for h in infra_hints):
            return ClaudeResult(error_kind=ErrorKind.INFRA, error_message=msg)
        # Try to still extract cost if there's JSON output (claude's own error)
        try:
            partial = json.loads(stdout)
            cost = partial.get("total_cost_usd", 0.0) or 0.0
            return ClaudeResult(
                stdout_json=partial, total_cost_usd=cost,
                error_kind=ErrorKind.TRANSIENT, error_message=msg,
            )
        except json.JSONDecodeError:
            return ClaudeResult(error_kind=ErrorKind.TRANSIENT, error_message=msg)

    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        return ClaudeResult(
            error_kind=ErrorKind.PARSE,
            error_message=f"outer JSON parse failed: {stdout[:200]}",
        )

    cost = outer.get("total_cost_usd", 0.0) or 0.0

    if outer.get("is_error"):
        return ClaudeResult(
            stdout_json=outer, total_cost_usd=cost,
            error_kind=ErrorKind.TRANSIENT,
            error_message=str(outer.get("result", "claude reported error"))[:300],
        )

    return ClaudeResult(stdout_json=outer, total_cost_usd=cost)


# ---------- Test hooks ----------

def _reset_cache_for_tests() -> None:
    """Called only from tests to clear cached state between runs."""
    global _cached_claude_path
    _cached_claude_path = None
    _active_procs.clear()
