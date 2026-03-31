# Decision Chronicle

Captures planning decisions, architecture choices, debugging context, and implementation rationale from coding sessions — writes them to searchable markdown files automatically.

It records both minds: the programmer's intuitions, pushback, and "wait, what about X?" moments, and the assistant's analysis, trade-off evaluations, and course corrections. Six months later, anyone reading the chronicle doesn't just see what was built — they see how it was thought through.

## The problem

You spend time planning: architecture decisions, stack choices, testing strategies. Then you delegate implementation, guiding it step by step. Everything lands in git. But the *reasoning* — trade-offs discussed, approaches rejected, the "why" behind the "what" — lives only in ephemeral chat sessions. After they end, that knowledge is gone.

## How it works

Nothing changes about your workflow. Work as usual, close the session.

1. **Hooks fire** on every prompt, response, and session end — logging events
2. **A background daemon** waits until all sessions are quiet for 5 minutes, then summarizes each session via `claude -p --bare` (uses your subscription tokens)
3. **A markdown file** appears per session with turn-by-turn log, decisions, narrative, problems solved, and more
4. **Next session** gets past decisions injected as context automatically

### Event capture

```
  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │  Project A   │  │  Project B   │  │  Project C   │
  │  (session 1) │  │  (session 2) │  │  (session 3) │
  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
         │                │                │
    SessionStart     UserPromptSubmit     Stop
    Stop             Stop              SessionEnd
    SessionEnd       SessionEnd
         │                │                │
         └────────┬───────┴────────┬───────┘
                  │    hooks fire   │
                  │    (async)      │
                  v                 v
       ~/.chronicle/events.jsonl (shared append-only queue)
       Each line: {"session_id":"...","hook_event_name":"...","cwd":"..."}
```

### Daemon processing pipeline

```
       events.jsonl
            │
            │  poll every 5s (byte-offset tracking)
            v
  ┌──────────────────────┐     UserPromptSubmit from
  │   DAEMON (singleton) │ <── ANY session resets the
  │                      │     global debounce timer
  │  pending_sessions:   │
  │    sid_1 -> event    │     Only fires when ALL
  │    sid_2 -> event    │     sessions are quiet for
  │                      │     5 minutes
  └──────────┬───────────┘
             │
      (5 min global silence)
             │
             v
  ┌──────────────────────┐
  │  Parallel processing  │
  │  (5 async workers)    │
  │                       │
  │  Per session:         │
  │  ┌─────────────────┐  │
  │  │ 1. Read JSONL   │  │     ~/.claude/projects/<slug>/<id>.jsonl
  │  │ 2. Parse turns  │  │     Extract user/assistant/tool timeline
  │  │ 3. Redact       │  │     API keys, tokens, PEM, JWTs, .env
  │  │ 4. Summarize    │  │     claude -p --model opus --bare
  │  │ 5. Return entry │  │     Returns (digest, entry) tuple
  │  └─────────────────┘  │
  └──────────┬────────────┘
             │
      sort by start_time
             │
             v
  ┌──────────────────────┐
  │  Write (chronological │     Entries appended to chronicle.md
  │  order guaranteed)    │     in session start_time order
  │                       │
  │  Per session:         │
  │  ├─ session .md file  │     ~/.chronicle/projects/<slug>/sessions/
  │  ├─ chronicle.md      │     ~/.chronicle/projects/<slug>/chronicle.md
  │  └─ processed marker  │     ~/.chronicle/.processed/<hash>
  └───────────────────────┘
```

### Context injection (next session)

```
  Developer starts new session
            │
            v
  ┌──────────────────────┐
  │  SessionStart hook    │     (sync — blocks until done)
  │                       │
  │  1. Log event         │
  │  2. Auto-spawn daemon │     (if not already running)
  │  3. Read recent .md   │     ~/.chronicle/projects/<slug>/sessions/
  │     titles            │
  │  4. Return as         │     {"additionalContext": "Previous sessions:
  │     additionalContext  │       - Wiring authentication hooks
  │                       │       - Migrating to async workers
  └───────────────────────┘       - Fixing race in batch processor"}
```

### Secret redaction pipeline

```
  Raw tool output from JSONL
            │
            v
  ┌──────────────────────┐
  │  Pattern scanner      │
  │                       │
  │  ├─ API keys          │     sk-, ghp_, AKIA, xoxb-
  │  ├─ Bearer tokens     │     Authorization: Bearer ...
  │  ├─ Private keys      │     -----BEGIN RSA PRIVATE KEY-----
  │  ├─ JWTs              │     eyJ...
  │  ├─ Connection URIs   │     postgres://user:pass@host/db
  │  ├─ Env var assigns   │     API_KEY=..., SECRET=..., PASSWORD=...
  │  └─ Sensitive files   │     .env, .pem, .key → full content redacted
  │                       │
  │  All replaced with    │
  │  [REDACTED]           │
  └──────────────────────┘
```

## Prerequisites

- **macOS or Linux** (Windows: use WSL)
- **Python 3.10+** (`python3 --version`)
- **Claude Code CLI** (`claude --version`)
- **Any Claude Code subscription** (summarization uses `claude -p --bare`, counts against your plan's token usage)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash
```

The script checks your platform, finds Python 3.10+, clones to `~/.chronicle/src`, creates a venv, configures hooks in `~/.claude/settings.json`, and sets secure permissions. Then restart your coding assistant.

### Update

```bash
# If installed via curl one-liner:
cd ~/.chronicle/src && git pull && chronicle reload

# Or just re-run the installer (it updates if already installed):
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash
```

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
    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "statusMessage": "Loading chronicle context..."}]}],
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}],
    "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}],
    "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "chronicle-hook", "async": true}]}]
  }
}
```

Hooks go inside a `"hooks": { ... }` wrapper per the [official docs](https://code.claude.com/docs/en/hooks-guide). Restart after editing.

</details>

## First run

```bash
# See what sessions you already have
chronicle query projects

# Process all past sessions across all projects
chronicle batch --workers 5

# Or just one project (use the folder name)
chronicle batch --project myproject --workers 5

# Preview without processing
chronicle batch --dry-run
```

## Usage

After setup, everything is automatic. The daemon processes sessions in the background. These commands are for browsing and manual processing:

```bash
# Browse
chronicle query sessions              # current project (from project dir)
chronicle query projects              # all projects
chronicle query timeline              # recent sessions
chronicle query search "auth"         # full-text search

# Process
chronicle batch --workers 5                                # all projects
chronicle batch --project myproject --workers 5            # one project (folder name)
chronicle batch --force --project myproject --workers 5    # reprocess after prompt changes

# Daemon
chronicle daemon --status
chronicle daemon --bg
chronicle daemon --stop
chronicle install-daemon              # auto-start on login (systemd/launchd)

# Maintenance
chronicle reload                      # reinstall + fix symlinks + reconfigure hooks
```

`--project` matches by folder name. `chronicle batch --project bada` matches any project whose path contains "bada". Run from anywhere.

Without `--force`, already-processed sessions are skipped. Use `--force` only after changing the summarization prompt or extraction logic.

## What gets captured

| Section | Description |
|---------|-------------|
| **Turn-by-turn log** | Every turn chronologically — prompts, responses, full Edit diffs, Write content, Bash commands, tool output |
| **Decisions** | Architecture choices with status (made/rejected/tentative), rationale, alternatives considered |
| **Narrative** | Chronological account written like an engineer explaining to a colleague |
| **Problems solved** | Symptom → diagnosis → fix → verification with exact error messages |
| **Developer reasoning** | Moments where you pushed back, reframed, or made judgment calls |
| **Follow-ups** | Clarifying questions: "wait, should we use X?", "what if we tried Y?" |
| **Architecture** | Project structure, patterns, data flow |
| **Planning** | Initial plan, how it evolved, work breakdown, what was deferred |
| **Technical details** | Stack, benchmarks, error messages, commands, config |

## Turns vs prompts

`682 turns, 94 prompts` means 682 total exchanges (user + assistant + tool results), but only 94 were things you typed. The rest were responses, tool outputs, and system messages.

## Configuration

`~/.chronicle/config.json` (auto-created):

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `"opus"` | Model for summarization |
| `concurrency` | `5` | Parallel workers |
| `min_turns_to_chronicle` | `1` | Skip sessions shorter than this |
| `poll_interval_seconds` | `5` | Daemon poll interval |
| `quiet_minutes` | `5` | Global debounce — minutes of silence before processing |
| `max_retries` | `3` | Give up on a session after this many failed attempts |
| `skip_projects` | `[]` | Project slugs to exclude |

## Where things live

```
~/.claude/projects/<slug>/*.jsonl     ← session data (written by Claude Code)
~/.chronicle/events.jsonl             ← hook event queue
~/.chronicle/projects/<slug>/
  ├── chronicle.md                    ← cumulative project log
  └── sessions/
      └── 2026-04-01_0611_abc12345_wiring-hooks.md
```

The `<slug>` is your project path with `/` replaced by `-` (underscores preserved).

## Security

**Secret redaction**: Tool outputs are scanned for known patterns before storage. Private keys, API keys (`sk-`, `ghp_`, `xoxb-`, `AKIA`), auth headers (`Bearer`), connection strings, JWTs, and env vars (`API_KEY=`, `SECRET=`, `PASSWORD=`) are replaced with `[REDACTED]`. Sensitive file types (`.env`, `.pem`, `.key`, `credentials`) get fully redacted Write content. User prompts are NOT redacted.

**File permissions**: `~/.chronicle/` is 0700 (owner-only), matching `~/.claude/`.

**Network**: `claude -p` sends the redacted transcript to the API via your subscription.

## How is this possible

Two things that Claude Code already provides:

1. **Session JSONL files** at `~/.claude/projects/<slug>/*.jsonl` — Claude Code writes every conversation turn here. Every prompt, response, tool call, tool result. We just read what it already writes.
2. **Hooks** in `~/.claude/settings.json` — Claude Code fires events at lifecycle points and runs our command. We just listen.

Chronicle is purely an observer. It does not change any default behavior:

- Async hooks run in the background — Claude Code doesn't wait for them
- Hooks don't return any `block`, `deny`, or `decision` — they just log an event and exit
- SessionStart injects past decisions as `additionalContext` — additive, doesn't replace anything
- The daemon reads JSONL files but never writes to `~/.claude/`
- The `claude -p --bare` summarization is a completely separate process

Claude Code behaves exactly the same with or without chronicle installed.

## Caveats

- **Uses your subscription tokens** — each session summarization is one `claude -p` call, comparable to sending a long message. Cost is minimal — a few sessions a day is negligible on any plan. No separate API key or billing needed.
- **Global debounce** — daemon waits until ALL sessions across ALL projects are quiet for 5 minutes before processing anything
- **Daemon auto-spawns** on SessionStart, auto-stops/restarts around `chronicle batch`
- **Transient failures retry** — rate limits don't mark sessions as done, gives up after `max_retries` attempts
- **Hook errors logged** — failures go to `~/.chronicle/hook-errors.log` instead of being silently swallowed
- **Ctrl+C is safe** — nothing gets marked as processed on interrupt

## Project structure

```
chronicle/
  hook.py                       # logs events, spawns daemon, injects past decisions
  daemon.py                     # global debounce, parallel dispatch, chronological writes
  extractor.py                  # JSONL → interleaved timeline with full tool details
  summarizer.py                 # LLM summarization, JSON extraction, markdown rendering
  storage.py                    # shared session writing, dedup markers, retry tracking
  filtering.py                  # shared session filtering logic
  batch.py                      # retroactive processing, parallel workers, --force
  query.py                      # search, timeline, project listing, session lookup
  config.py                     # paths, defaults, permissions
  install_hooks.py              # merges hooks into settings.json (preserves existing)
  __main__.py                   # CLI: daemon, batch, query, install-daemon, reload
  chronicle-daemon.service      # systemd unit for Linux auto-start
tests/                          # 70 tests covering daemon, batch, hooks, extraction, storage, filtering
```
