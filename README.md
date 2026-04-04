# Decision Chronicle

Captures planning decisions, architecture choices, debugging context, and implementation rationale from coding sessions — writes them to searchable markdown files automatically.

It records both minds: the programmer's intuitions, pushback, and "wait, what about X?" moments, and the assistant's analysis, trade-off evaluations, and course corrections. Six months later, anyone reading the chronicle doesn't just see what was built — they see how it was thought through.

## The problem

You spend time planning: architecture decisions, stack choices, testing strategies. Then you delegate implementation, guiding it step by step. Everything lands in git. But the *reasoning* — trade-offs discussed, approaches rejected, the "why" behind the "what" — lives only in ephemeral chat sessions. After they end, that knowledge is gone.

## How it works

Nothing changes about your workflow. Work as usual, close the session.

1. **Hooks fire** on every prompt, response, and session end — logging events
2. **A background daemon** waits until all sessions are quiet for 5 minutes, then summarizes each session via `claude -p` (uses your subscription)
3. **One chronicle.md per project** — full session content stacked chronologically with a timeline table at top, plus individual session files
4. **Next session** gets past decisions injected as context automatically

### Architecture

```mermaid
flowchart LR
    subgraph sessions["Claude Code Sessions"]
        A["Project A"]
        B["Project B"]
        C["Project C"]
    end

    subgraph hooks["Hook Layer"]
        EQ[("events.jsonl")]
    end

    subgraph daemon["Background Daemon"]
        DB["Global Debounce"]
        PW["Parallel Workers x5"]
    end

    subgraph pipeline["Processing"]
        EX["Extract JSONL"]
        RD["Redact Secrets"]
        SM["claude -p"]
    end

    subgraph output["Output"]
        SF["Session .md"]
        CM["chronicle.md"]
    end

    A & B & C -->|"hooks fire"| EQ
    EQ -->|"poll 5s"| DB
    DB -->|"5 min quiet"| PW
    PW --> EX --> RD --> SM
    SM --> SF & CM
    CM -.->|"inject context"| sessions
```

### Event flow

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant H as Hook
    participant EQ as events.jsonl
    participant D as Daemon
    participant API as claude -p
    participant FS as Markdown Files

    CC->>H: SessionStart
    H->>EQ: append event
    H->>D: spawn (if not running)
    H-->>CC: additionalContext (past decisions)

    CC->>H: UserPromptSubmit
    H->>EQ: append event
    Note over D: debounce timer resets

    CC->>H: Stop / SessionEnd
    H->>EQ: append event

    Note over D: 5 min silence...
    D->>EQ: read new events
    D->>D: extract + redact
    D->>API: summarize transcript
    API-->>D: structured JSON
    D->>FS: write session.md + chronicle.md
```

### Secret redaction

All tool outputs pass through a pattern scanner before storage:

| Pattern | Examples |
|---------|----------|
| API keys | `sk-`, `ghp_`, `AKIA`, `xoxb-` |
| Auth headers | `Bearer ...` |
| Private keys | `-----BEGIN RSA PRIVATE KEY-----` |
| JWTs | `eyJ...` |
| Connection URIs | `postgres://user:pass@host/db` |
| Env var assignments | `API_KEY=...`, `SECRET=...`, `PASSWORD=...` |
| Sensitive files | `.env`, `.pem`, `.key` — full content redacted |

## Prerequisites

- **macOS or Linux** (Windows: use WSL)
- **Python 3.10+** (`python3 --version`)
- **Claude Code CLI** (`claude --version`)
- **Claude Code subscription** (Pro, Max, or Teams — summarization uses `claude -p`)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash
```

The script checks your platform, finds Python 3.10+, clones to `~/.chronicle/src`, creates a venv, configures hooks in `~/.claude/settings.json`, and sets secure permissions. Restart Claude Code to activate.

To update, just re-run the same command. It handles dirty install directories automatically.

<details><summary>Manual install</summary>

```bash
git clone https://github.com/ehzawad/claudetalktoclaude.git
cd claudetalktoclaude
python3 -m venv .venv
.venv/bin/pip install -e .
mkdir -p ~/.local/bin
ln -sf "$(pwd)/.venv/bin/chronicle-hook" ~/.local/bin/chronicle-hook
ln -sf "$(pwd)/.venv/bin/chronicle" ~/.local/bin/chronicle
```

Then add hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook"}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}],
    "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}]
  }
}
```

</details>

## First run

```bash
chronicle query projects               # see what sessions exist
chronicle batch --workers 5            # process all past sessions
chronicle batch --project myproject    # just one project
chronicle batch --dry-run              # preview without processing
```

## Usage

After setup, everything is automatic. These commands are for browsing and manual processing:

```bash
# Browse
chronicle query sessions              # current project
chronicle query projects              # all projects
chronicle query timeline              # recent sessions
chronicle query search "auth"         # full-text search

# Rewind — navigate session history
chronicle rewind                      # numbered session list
chronicle rewind 3                    # view session #3
chronicle rewind --since 2            # sessions #2 through latest
chronicle rewind --diff 3             # what was NEW in session #3
chronicle rewind --summary 2          # AI-summarize from #2 onward

# Process
chronicle batch --workers 5           # all projects
chronicle batch --force --workers 5   # reprocess everything

# Daemon
chronicle daemon --status
chronicle daemon --stop
chronicle install-daemon              # auto-start on login

# Maintenance
chronicle --version
chronicle reload                      # reinstall + restart daemon
```

`--project` matches by folder name substring. Run from anywhere.

## What gets captured

| Section | Description |
|---------|-------------|
| **Turn-by-turn log** | Every turn — prompts, responses, Edit diffs, Write content, Bash commands, tool output |
| **Decisions** | Architecture choices with status (made/rejected/tentative), rationale, alternatives |
| **Narrative** | Chronological account, written like an engineer explaining to a colleague |
| **Problems solved** | Symptom, diagnosis, fix, verification with exact error messages |
| **Developer reasoning** | Moments where you pushed back, reframed, or made judgment calls |
| **Follow-ups** | Clarifying questions and what changed as a result |
| **Architecture** | Project structure, patterns, data flow |
| **Planning** | Initial plan, how it evolved, what was deferred |
| **Technical details** | Stack, benchmarks, errors, commands, config |

## Configuration

`~/.chronicle/config.json` (auto-created):

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `"opus"` | Model for summarization |
| `concurrency` | `5` | Parallel workers |
| `poll_interval_seconds` | `5` | Daemon poll interval |
| `quiet_minutes` | `5` | Global debounce — minutes of silence before processing |
| `max_retries` | `3` | Give up after N failed summarization attempts |
| `skip_projects` | `[]` | Project slugs to exclude |

## Where things live

```
~/.claude/projects/<slug>/*.jsonl     <- session data (Claude Code writes these)
~/.chronicle/
  ├── events.jsonl                    <- hook event queue
  ├── config.json                     <- configuration
  ├── daemon.pid                      <- singleton lock
  └── projects/<slug>/
      ├── chronicle.md                <- cumulative project log
      └── sessions/
          └── 2026-04-01_abc12345_wiring-hooks.md
```

The `<slug>` is your project path with `/` replaced by `-`.

## Security

**Secret redaction** — tool outputs are scanned for known patterns before storage. API keys, auth headers, private keys, JWTs, connection strings, and env var assignments are replaced with `[REDACTED]`. Sensitive file types (`.env`, `.pem`, `.key`) get fully redacted content. User prompts are not redacted.

**Subscription routing** — `claude -p` subprocess calls strip `ANTHROPIC_API_KEY` from the environment so summarization always routes through your paid subscription instead of API credits ([anthropics/claude-code#2051](https://github.com/anthropics/claude-code/issues/2051)).

**File permissions** — `~/.chronicle/` is `0700` (owner-only), matching `~/.claude/`.

**Observer only** — chronicle never writes to `~/.claude/`, never blocks hooks, never modifies Claude Code behavior. The only sync hook (SessionStart) injects past decision titles as additive context.

## How is this possible

Two things Claude Code already provides:

1. **Session JSONL files** at `~/.claude/projects/<slug>/*.jsonl` — every conversation turn. We just read them.
2. **Hooks** in `~/.claude/settings.json` — lifecycle events. We just listen.

## Caveats

- **Uses your subscription** — each summarization is one `claude -p` call, comparable to a long message. Cost is negligible on any plan.
- **Global debounce** — waits until ALL sessions across ALL projects are quiet for 5 minutes
- **Daemon auto-spawns** on SessionStart if not running
- **Transient failures retry** — up to `max_retries` attempts per session
- **Ctrl+C safe** — already-processed sessions are skipped on retry

## Project structure

```
chronicle/
  hook.py             # event logging, daemon spawn, context injection
  daemon.py           # polling, global debounce, parallel dispatch
  extractor.py        # JSONL parsing, secret redaction, timeline building
  summarizer.py       # claude -p subprocess, JSON extraction, markdown rendering
  storage.py          # atomic writes, dedup, retry tracking, chronicle.md management
  filtering.py        # session skip logic (self-detection, project exclusion)
  batch.py            # retroactive bulk processing
  query.py            # search, timeline, project listing
  rewind.py           # numbered session navigator with --diff, --since, --summary
  config.py           # paths, defaults, permissions
  install_hooks.py    # idempotent hook configuration
  __main__.py         # CLI dispatcher + install-daemon + reload
```
