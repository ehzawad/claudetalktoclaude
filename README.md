# Decision Chronicle

Records the *reasoning* behind your coding sessions — planning discussions, trade-offs, rejected approaches, debugging context — as searchable markdown. Six months later, the chronicle shows not just what was built, but how it was thought through.

> **Default install is foreground mode.** Chronicle always records hook events and injects past-session context into new Claude Code sessions, but **does NOT run `claude -p` or spend tokens** unless you explicitly run `chronicle process`, `chronicle insight`, `chronicle story`, or `chronicle rewind --summary`. Background auto-summarization is opt-in via `chronicle install-daemon`.

---

## Quick start

**Prerequisites:** macOS (Apple Silicon) or Linux (x86_64) · Claude Code CLI · Claude subscription (Pro / Max / Teams).

Chronicle ships as a prebuilt self-contained binary. No Python, venv, or system package dependencies on the target machine.

```bash
# Default (foreground — zero passive token burn)
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash

# Background mode (daemon auto-summarizes after 5 min quiet)
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash && chronicle install-daemon
```

Pin a specific release: `CHRONICLE_VERSION=v0.8.0 curl ... | bash`. Upgrade: `chronicle update`.

Restart Claude Code to activate hooks. Then:

```bash
chronicle doctor                    # verify everything resolves
chronicle query projects            # show per-project session counts
chronicle process --workers 5       # summarize pending sessions (foreground)
```

Switch modes anytime:

```bash
chronicle install-daemon            # → background
chronicle uninstall-daemon          # → foreground
```

---

## Processing modes

| | Foreground (default) | Background (opt-in) |
|---|---|---|
| Hooks record session events | yes | yes |
| Past-session titles injected into new sessions | yes | yes |
| Auto-summarization | **no** | after 5 min of quiet |
| Passive token burn | zero | per-session |
| Runs a service | **no** | launchd (macOS) / systemd --user (Linux) |
| Enable | (default) | `chronicle install-daemon` |
| Disable | (default) | `chronicle uninstall-daemon` |

Mode is stored in `~/.chronicle/config.json` under `processing_mode`. `chronicle doctor` reports current mode plus any drift (e.g., config says foreground but a stale daemon is running).

---

## Daily use

### Browse

```bash
chronicle query projects              # per-project OK / Pend / Fail counts
chronicle query timeline              # recent sessions across all projects
chronicle query sessions              # current project's chronicle
chronicle query search "auth"         # full-text across all chronicles
```

### Process (summarize sessions)

```bash
chronicle process --workers 5                  # pending sessions
chronicle process --project slug               # substring match against slug
chronicle process --force --workers 5          # reprocess successes
chronicle process --retry-failed --workers 5   # retry terminal failures
chronicle process --dry-run                    # preview only
```

### Analyze (existing chronicle data)

```bash
chronicle rewind                      # numbered session list
chronicle rewind <N>                  # show session #N
chronicle rewind --since <N>          # sessions #N through latest
chronicle rewind --diff <N>           # what was NEW in session #N
chronicle rewind --summary <N>        # AI-summarize #N onward (calls claude -p)
chronicle rewind --delete <N>         # remove one session's records
chronicle rewind --prune              # delete sessions with 0 decisions
chronicle insight [project]           # HTML dashboard (calls claude -p)
chronicle story [project]             # unified narrative md (calls claude -p)
```

### Diagnose / mode switching

```bash
chronicle doctor                      # human-readable diagnostic
chronicle doctor --json               # machine-readable (CI-friendly)
chronicle install-daemon              # switch to background mode
chronicle uninstall-daemon            # switch to foreground mode
chronicle update                      # fetch + install the latest release, restart daemon if running
chronicle --version
```

> Commands that spawn `claude -p` are: `process`, `insight`, `story`, `rewind --summary`, and the background daemon. Nothing else spends tokens.

---

## Concepts

**Session** — one conversation with Claude Code. Stored as `~/.claude/projects/<slug>/<session-id>.jsonl`.

**Project slug** — the working directory with `/` replaced by `-`. For example `/Users/alice/my-api` → `-Users-alice-my-api`.

**Substring project matching** — `--project <name>` matches any slug containing `<name>`. So `--project my-api` finds `-Users-alice-my-api`. See all your slugs: `chronicle query projects`.

**Marker state** — each session is in exactly one state: unprocessed (no marker), success (`.processed/<hash>`), or failed (`.failed/<hash>.json` with `terminal` flag + attempt counter). See [State and failures](#state-and-failures).

---

## How processing works

Both foreground and background use the same pipeline. The only difference is *who triggers it*.

```mermaid
flowchart TB
    subgraph hooks["Hooks (always)"]
        SS["SessionStart: inject past titles<br/>(+ spawn daemon if background)"]
        LOG["UserPromptSubmit / Stop / SessionEnd:<br/>append to ~/.chronicle/events.jsonl"]
    end

    TRIG{"Trigger?"}

    subgraph fg["Foreground (explicit)"]
        FG["chronicle process / insight /<br/>story / rewind --summary"]
    end

    subgraph bg["Background (daemon)"]
        BG["Debounce 5 min quiet<br/>+ periodic scan"]
    end

    subgraph pipeline["claude_cli.spawn_claude"]
        RESOLVE["resolve claude binary<br/>(shutil.which + fallback dirs)"]
        ENV["strip ANTHROPIC_API_KEY /<br/>AUTH_TOKEN / BASE_URL"]
        SPAWN["claude -p --json-schema<br/>--effort max<br/>--fallback-model sonnet"]
        CLASSIFY["classify result:<br/>INFRA / TRANSIENT / PARSE"]
    end

    subgraph write["Write (under processing.lock)"]
        OK[".processed/&lt;hash&gt;<br/>+ sessions/*.md<br/>+ chronicle.md"]
        FAIL[".failed/&lt;hash&gt;.json<br/>{attempts, terminal, error}"]
    end

    CC["Claude Code session"] --> hooks
    hooks --> TRIG
    TRIG -->|"user ran a command"| FG
    TRIG -->|"daemon tick + quiet window"| BG
    FG --> RESOLVE
    BG --> RESOLVE
    RESOLVE --> ENV --> SPAWN --> CLASSIFY
    CLASSIFY -->|success| OK
    CLASSIFY -->|transient / parse| FAIL
    CLASSIFY -.->|INFRA| config_fix["user fixes PATH / auth;<br/>no retry budget consumed"]
```

**Five-step invariant** on every summarization (foreground or background):

1. Extract the session JSONL, redact secrets (API keys, tokens, JWTs, connection URIs, `.env`/`.pem`/`.key` contents).
2. Resolve the `claude` binary; build a subprocess env with auth-routing vars stripped.
3. Invoke `claude -p` under the processing lock (`~/.chronicle/processing.lock`).
4. Classify the outcome: success / transient / parse / infra.
5. Write `.processed/` (success) or `.failed/` (transient + terminal flag).

---

## Background mode internals

Only relevant if you `chronicle install-daemon`.

- **Debounce.** The daemon waits until ALL sessions across ALL projects have been quiet for `quiet_minutes` (default 5) before processing anything. This prevents the daemon from competing with your active coding session for the same subscription rate limits.
- **Periodic scan.** Every `scan_interval_minutes` (default 30) the daemon walks `~/.claude/projects/` and queues any session JSONL that has no `.processed` or `.failed` marker — picks up sessions that pre-date the install or were missed while the daemon was down.
- **Parallel workers.** Up to `concurrency` (default 5) summarizations run concurrently via `asyncio.Semaphore`. Each worker is an independent `claude -p` subprocess.
- **Singleton.** Single daemon enforced by `fcntl.flock` on `~/.chronicle/daemon.pid` plus inode-validation to detect PID-file replacement.
- **Graceful shutdown.** On SIGTERM/SIGINT/SIGHUP, the daemon terminates in-flight `claude` subprocesses (SIGTERM then SIGKILL after 5s) before exiting.
- **Service-manager-aware batch.** `chronicle process` pauses the service (`launchctl bootout` / `systemctl --user stop`) and holds the processing lock, then resumes after.
- **Self-disable.** If config says foreground but the service respawned the daemon anyway, the daemon idles instead of exiting — avoids a KeepAlive restart loop.

---

## State and failures

### Marker state machine

```mermaid
stateDiagram-v2
    [*] --> Unprocessed: new JSONL in ~/.claude/projects/

    Unprocessed --> Success: claude -p ok
    Unprocessed --> Retriable: transient / parse error

    Retriable --> Success: retry ok
    Retriable --> Retriable: another transient
    Retriable --> Terminal: attempts == max_retries

    Success: .processed/&lt;hash&gt;<br/>+ sessions/*.md
    Retriable: .failed/&lt;hash&gt;.json<br/>terminal=false<br/>attempts &lt; max
    Terminal: .failed/&lt;hash&gt;.json<br/>terminal=true

    Terminal --> Unprocessed: chronicle process --retry-failed
    Success --> Unprocessed: chronicle process --force
```

**Infra errors don't enter this state machine.** A missing `claude` binary, auth failure, or permission error is a daemon-level problem, not a per-session one — no marker is written, no retry budget consumed.

### Error classification

```mermaid
flowchart LR
    CALL["spawn_claude()"] --> RC{"returncode<br/>== 0 ?"}
    RC -- no --> ERRSNIFF{"stderr has<br/>auth / no-such-file?"}
    ERRSNIFF -- yes --> INFRA["ErrorKind.INFRA<br/>(NOT counted —<br/>user fixes config)"]
    ERRSNIFF -- no --> TRANS
    RC -- yes --> PARSE{"outer JSON<br/>parseable ?"}
    PARSE -- no --> PERR["ErrorKind.PARSE"]
    PARSE -- yes --> ISERR{"outer.is_error ?"}
    ISERR -- yes --> TRANS["ErrorKind.TRANSIENT"]
    ISERR -- no --> OK["Success"]

    PERR --> COUNT["counts against max_retries"]
    TRANS --> COUNT
```

To inspect current failure state: `chronicle doctor` (or `chronicle doctor --json`). To retry after fixing the underlying issue: `chronicle process --retry-failed --workers 5`.

---

## Output and storage

Each project gets up to three views:

| Output | What it is | How to access |
|---|---|---|
| `chronicle.md` | Cumulative session records per project | `chronicle query sessions` |
| `insight.html` | LLM-generated HTML dashboard with charts + narrative | `chronicle insight [project]` |
| `story.md` | Unified chronological project narrative | `chronicle story [project]` |

### Where things live

```
~/.claude/projects/<slug>/*.jsonl       # Claude Code session transcripts (source)

~/.chronicle/
  events.jsonl                          # hook event journal
  events.offset                         # daemon read position (background only)
  config.json                           # processing_mode + model + concurrency + …
  daemon.pid                            # singleton lock (background only)
  daemon.log                            # daemon stdout/stderr (background only)
  processing.lock                       # mutex between daemon and `chronicle process`
  .processed/<hash>                     # success marker
  .failed/<hash>.json                   # failure record (attempts, terminal, error)
  projects/<slug>/
    chronicle.md                        # cumulative project log
    insight.html                        # `chronicle insight` output
    story.md                            # `chronicle story` output
    sessions/
      2026-04-01_0611_abc12345_title.md # per-session record

~/Library/LaunchAgents/com.chronicle.daemon.plist   # macOS service (background only)
~/.config/systemd/user/chronicle-daemon.service     # Linux service (background only)
```

### What gets captured in session `.md`

Turn-by-turn log · decisions with status + rationale + alternatives · problems solved (symptom/diagnosis/fix/verification) · developer reasoning moments · follow-up questions · architecture patterns · planning evolution · technical details (stack, errors, commands, config) · per-session cost.

---

## Configuration

`~/.chronicle/config.json` (auto-created):

| Key | Default | Scope | Description |
|---|---|---|---|
| `processing_mode` | `"foreground"` | both | `"foreground"` or `"background"`. Set via `chronicle install-daemon` / `uninstall-daemon`. |
| `model` | `"opus"` | both | Primary model for summarization. |
| `fallback_model` | `"sonnet"` | both | Auto-fallback when primary overloaded. |
| `max_retries` | `3` | both | Transient failures flip terminal after N attempts. |
| `skip_projects` | `[]` | both | Project slugs (substrings) to exclude. |
| `concurrency` | `5` | background | Parallel workers in the daemon. (`chronicle process --workers` overrides for that invocation.) |
| `poll_interval_seconds` | `5` | background | Daemon event-journal poll cadence. |
| `quiet_minutes` | `5` | background | Debounce — minutes of silence before daemon processes. |
| `scan_interval_minutes` | `30` | background | How often the daemon scans for un-evented sessions. |

---

## Security

- **Secret redaction.** All tool outputs pass through a pattern scanner before any markdown is written. API keys (`sk-`, `ghp_`, `AKIA`, `xoxb-`), auth headers (`Bearer …`), private keys (`-----BEGIN …`), JWTs (`eyJ…`), connection URIs (`postgres://user:pass@…`), and env-var assignments (`API_KEY=…`, `SECRET=…`) are replaced with `[REDACTED]`. `.env`, `.pem`, and `.key` file content is fully redacted.
- **Subscription routing.** Every `claude -p` subprocess call strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` from the environment — summarization always routes through your Claude.ai subscription, never API credits or a proxy gateway ([anthropics/claude-code#2051](https://github.com/anthropics/claude-code/issues/2051)).
- **File permissions.** `~/.chronicle/` is `0700` (owner-only), matching `~/.claude/`.
- **Observer-only.** Chronicle never writes to `~/.claude/`, never blocks a hook, never modifies Claude Code behavior. The only effect on the active session is the `additionalContext` injection of past session titles on SessionStart. All deletion operations (`rewind --delete`, `--prune`) only touch chronicle's own markdown and markers — the original JSONL in `~/.claude/projects/` stays.

---

## Troubleshooting

Run `chronicle doctor` first. It reports:

- Resolved `claude` binary path (or flags it as missing)
- Effective PATH
- Current mode + daemon status + service drift warnings
- Processing lock state
- Per-project processed / pending / terminal-failure counts

Common fixes:

- **`claude` not found.** Install the Claude Code CLI, or ensure it's on the daemon's PATH. `chronicle install-daemon` bakes PATH into the launchd plist / systemd unit so minimal service-manager envs (`/usr/bin:/bin:/usr/sbin:/sbin`) don't cause `FileNotFoundError`.
- **Mode drift warning.** Config says one mode but service state says another. `chronicle install-daemon` / `uninstall-daemon` reconciles.
- **Terminal failures after fixing a config issue.** `chronicle process --retry-failed --workers 5`.
- **Ubuntu background mode survives logout.** Run once: `sudo loginctl enable-linger "$USER"`.
- **Scripted health check.** `chronicle doctor --json` emits a schema-versioned document with a top-level `ok: bool`; exit code is 0 if healthy, 1 if drift detected.

---

## Developer map

```
chronicle/
  __main__.py          # CLI dispatcher (process / query / rewind / insight /
                       #   story / doctor / install-daemon / uninstall-daemon /
                       #   daemon / install-hooks / update)
  _entrypoint.py       # PyInstaller busybox dispatcher — argv[0] picks
                       #   between chronicle CLI and chronicle-hook
  hook.py              # hook dispatcher — logs events, injects context,
                       #   spawns daemon (background only)
  daemon.py            # background poll loop, debounce, scan, parallel workers
  batch.py             # `chronicle process` — service-manager-aware batch
  summarizer.py        # build prompt + parse structured_output → ChronicleEntry
  extractor.py         # JSONL → SessionDigest + timeline (with secret redaction)
  storage.py           # marker layout (.processed, .failed) + chronicle.md writes
  filtering.py         # should_skip: success / terminal / skip-project / self
  query.py             # query projects / timeline / sessions / search
  rewind.py            # numbered navigator — view, diff, summarize, delete, prune
  insight.py           # LLM-generated HTML dashboard
  story.py             # LLM-generated unified narrative
  doctor.py            # diagnostic (text + --json)
  claude_cli.py        # resolve claude binary, env sanitization, spawn wrapper,
                       #   error classification, subprocess registry
  service.py           # launchd plist / systemd unit install / pause / resume,
                       #   mode-drift detection
  locks.py             # fcntl helpers: singleton daemon lock + processing mutex
  mode.py              # processing_mode get/set (config is authoritative)
  config.py            # paths + defaults
  install_hooks.py     # idempotent ~/.claude/settings.json hook merge
```

Tests: `tests/unit/` (per-module) + `tests/functional/` (subprocess-level end-to-end with a fake `claude` stub). Runs in a few seconds — see `pytest -q` for the current count.
