# Decision Chronicle

Captures planning decisions, architecture choices, debugging context, and implementation rationale from coding sessions — writes them to searchable markdown files.

It records both minds: the programmer's intuitions, pushback, and "wait, what about X?" moments, and the assistant's analysis, trade-off evaluations, and course corrections. Six months later, anyone reading the chronicle doesn't just see what was built — they see how it was thought through.

> **Default install is foreground mode.** Chronicle records hook events and injects past-session context into new Claude Code sessions, but **does NOT run `claude -p` or spend tokens** unless you explicitly run `chronicle process`, `chronicle insight`, `chronicle story`, or `chronicle rewind --summary`. Automatic background summarization is opt-in via `chronicle install-daemon` and will passively spend tokens after quiet periods.

## The problem

You spend time planning: architecture decisions, stack choices, testing strategies. Then you delegate implementation, guiding it step by step. Everything lands in git. But the *reasoning* — trade-offs discussed, approaches rejected, the "why" behind the "what" — lives only in ephemeral chat sessions. After they end, that knowledge is gone.

## Two processing modes

Chronicle runs in one of two modes. **Foreground is the default** — it does not burn tokens passively.

| | Foreground (default) | Background |
|---|---|---|
| Hooks record session events | yes | yes |
| Past session titles injected into new sessions | yes | yes |
| Auto-summarization | **no** | after 5 min of quiet |
| Passive token burn | zero | per-session |
| Runs | launchd/systemd service | none |
| Switch | `chronicle install-daemon` | `chronicle uninstall-daemon` |

In **foreground mode**, summarization happens only when you explicitly run `chronicle process`, `chronicle insight`, `chronicle story`, or `chronicle rewind --summary`. This protects token budgets.

In **background mode**, a daemon (launchd on macOS, systemd --user on Linux) auto-summarizes closed sessions after 5 minutes of idle. Enable it with `chronicle install-daemon` when you want hands-off operation.

Diagnose your setup with `chronicle doctor`.

## Concepts

### What is a session?

A **session** is one conversation with Claude Code. You open a terminal, run `claude`, ask it to do something, maybe go back and forth a few times, then close it. That's one session. Claude Code stores the full transcript as a JSONL file at `~/.claude/projects/<slug>/<session-id>.jsonl`.

### What is a project?

A **project** is a working directory you've used Claude Code in. Chronicle identifies projects by their **slug** — the directory path with `/` replaced by `-`:

For example, if your username is `alice`:

| Working directory | Project slug |
|---|---|
| `/home/alice/my-api` | `-home-alice-my-api` |
| `/home/alice/projects/webapp` | `-home-alice-projects-webapp` |
| `/home/alice/ml/whisper-fine-tune` | `-home-alice-ml-whisper-fine-tune` |

### How project names work in commands

You can pass **any substring** that appears in the slug — the folder name, a partial path, or any part of the slug. Chronicle scans all project slugs and picks the one that contains your string:

```
chronicle insight api           # "api" is in -home-alice-my-api           ✓
chronicle insight my-api        # "my-api" is in -home-alice-my-api        ✓
chronicle insight alice-my      # "alice-my" is in -home-alice-my-api      ✓
chronicle story whisper         # "whisper" is in -home-alice-ml-whisper-fine-tune  ✓
chronicle process --project web # "web" is in -home-alice-projects-webapp  ✓
```

If you omit the project name, `insight` and `story` use your current working directory. `chronicle process` without `--project` processes all projects. See all your slugs with `chronicle query projects`.

**Note:** `insight`, `story`, and `query sessions` use the **first** matching slug (sorted alphabetically). `process --project` operates on **all** matching slugs. Use a specific enough substring to avoid ambiguity.

## How it works

In **both modes**, hooks fire on every prompt, response, and session end — logging events to `events.jsonl` and injecting past session titles as context into new sessions.

**Foreground mode** stops there. You run `chronicle process` (or `insight` / `story` / `rewind --summary`) when you want summaries. Each command:
1. Scans `~/.claude/projects/<slug>/*.jsonl` for sessions without a success marker.
2. Extracts each JSONL, redacts secrets, sends to `claude -p --json-schema --effort max --fallback-model sonnet --no-session-persistence` (stripping `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` to route through your subscription).
3. Writes the resulting per-session `.md` plus a cumulative `chronicle.md` per project.
4. Infra errors (missing binary, auth failure) are classified separately from transient errors so a config problem doesn't burn the retry budget.

**Background mode** adds a daemon that does the same processing automatically after the [Global debounce](#global-debounce) and through [Parallel workers](#parallel-workers). `chronicle install-daemon` installs + starts the service. `chronicle process` still works manually; it pauses the daemon service (via `launchctl bootout` / `systemctl --user stop`) and holds `~/.chronicle/processing.lock` while running so the two never race.

All state:
- `~/.chronicle/.processed/<hash>` — success marker (per-session `.md` written)
- `~/.chronicle/.failed/<hash>.json` — failure record with `{attempts, terminal, last_error_kind, last_error_message}`. `terminal=true` after `max_retries`, requires `chronicle process --retry-failed` to retry.
- `~/.chronicle/processing.lock` — mutex between daemon and batch

### Global debounce

The daemon doesn't process sessions immediately. It waits until you're done working — **all** sessions across **all** projects must be quiet for 5 minutes. This prevents the daemon from eating your API capacity while you're actively coding.

```mermaid
sequenceDiagram
    participant You as You (coding)
    participant D as Daemon
    participant API as claude -p

    Note over You: Working in Project A
    You->>D: UserPromptSubmit (Project A)
    Note over D: Timer: 0 min

    You->>D: UserPromptSubmit (Project A)
    Note over D: Timer resets: 0 min

    You->>D: Stop (Project A, session done)
    Note over D: Timer: 0 min... 1 min... 2 min...

    Note over You: Switch to Project B
    You->>D: UserPromptSubmit (Project B)
    Note over D: Timer resets: 0 min !!
    Note over D: (Project A still waiting)

    You->>D: Stop (Project B, session done)
    Note over D: Timer: 0 min... 1 min... 2 min...

    Note over You: Go get coffee

    Note over D: 3 min... 4 min... 5 min
    Note over D: All quiet for 5 minutes!
    Note over D: NOW process both sessions

    par Project A
        D->>API: summarize Project A session
        API-->>D: structured JSON
    and Project B
        D->>API: summarize Project B session
        API-->>D: structured JSON
    end

    Note over D: Both done. Back to polling.
```

Why? You're on a Claude Max subscription. If the daemon started summarizing while you're still coding, it would compete with your active session for the same rate limits. The 5-minute quiet window means it only works when you're idle.

### Parallel workers

When the debounce fires, the daemon doesn't process sessions one at a time. It runs up to 5 simultaneously using an asyncio semaphore:

```mermaid
flowchart TB
    READY["Debounce fired:<br/>8 sessions pending"]

    READY --> SEM["Semaphore (max 5 concurrent)"]

    SEM --> W1["Worker 1<br/>Project A / session abc"]
    SEM --> W2["Worker 2<br/>Project A / session def"]
    SEM --> W3["Worker 3<br/>Project B / session ghi"]
    SEM --> W4["Worker 4<br/>Project C / session jkl"]
    SEM --> W5["Worker 5<br/>Project D / session mno"]
    SEM -.->|"waiting"| W6["Session pqr"]
    SEM -.->|"waiting"| W7["Session stu"]
    SEM -.->|"waiting"| W8["Session vwx"]

    W1 --> DONE1["done → write .md"]
    W2 --> DONE2["done → write .md"]

    DONE1 --> FREE1["Slot freed"]
    DONE2 --> FREE2["Slot freed"]
    FREE1 --> W6
    FREE2 --> W7

    subgraph each["What each worker does"]
        direction LR
        E["Read .jsonl"] --> R["Redact secrets"] --> C["claude -p<br/>--json-schema"] --> W["Write session.md<br/>+ chronicle.md"]
    end
```

Each worker is an independent `claude -p` subprocess. They don't share state. When one finishes and frees a slot, the next pending session starts. All results are written chronologically regardless of which worker finished first.

### Periodic scan

The daemon also scans for sessions it doesn't know about — sessions that happened before chronicle was installed and never triggered any hooks:

```mermaid
flowchart TB
    TIMER["Every 30 minutes"]

    TIMER --> WALK["Walk ~/.claude/projects/*/"]
    WALK --> EACH{"For each *.jsonl file"}

    EACH --> CHECK1{"Already in<br/>pending queue?"}
    CHECK1 -->|yes| SKIP1["Skip"]
    CHECK1 -->|no| CHECK2{"Already<br/>chronicled?<br/>(.processed/ marker)"}
    CHECK2 -->|yes| SKIP2["Skip"]
    CHECK2 -->|no| QUEUE["Add to pending<br/>as synthetic 'Stop' event"]

    QUEUE --> DEBOUNCE["Enters normal debounce<br/>→ processed after 5 min quiet"]
```

This means you can install chronicle on a machine with months of Claude Code history and the daemon will process all of it automatically — no need to run `chronicle process` manually. The backlog is processed in one batch (throttled to 5 concurrent workers) the next time the debounce fires.

---

## Architecture

### System overview — the two modes

Mode is orthogonal. Hooks behave identically. The daemon is opt-in.

```mermaid
flowchart TB
    subgraph cc["Claude Code sessions"]
        S["Any session"]
    end

    subgraph hooks["Hooks (hook.py)"]
        SS["SessionStart (sync):<br/>inject past titles"]
        LOG["UserPromptSubmit / Stop / SessionEnd:<br/>append to events.jsonl"]
    end

    MODE{"processing_mode ?"}

    subgraph fg["Foreground (default)"]
        FG1["Nothing auto-processes"]
        FG2["User runs explicit command:<br/>chronicle process | insight | story"]
    end

    subgraph bg["Background (chronicle install-daemon)"]
        D1["Daemon polls events.jsonl"]
        D2["Debounce 5 min quiet<br/>+ periodic 30 min scan"]
        D3["Semaphore: 5 parallel workers"]
    end

    PIPE["Summarization pipeline<br/>(claude_cli.spawn_claude)"]
    OUT["Per-session .md<br/>+ chronicle.md<br/>+ .processed/ OR .failed/"]

    S --> SS & LOG
    SS -.->|context only| S
    LOG --> MODE
    MODE -->|foreground| fg
    MODE -->|background| bg
    FG2 --> PIPE
    D3 --> PIPE
    PIPE --> OUT
```

### Processing mode gate

In foreground mode, nothing calls `claude -p` automatically. The daemon, if present, detects the mode and idles.

```mermaid
sequenceDiagram
    participant User
    participant Hook as Hook (always runs)
    participant Cfg as config.json
    participant Daemon as Daemon<br/>(background only)
    participant API as claude -p

    Note over User,API: Session in progress
    Hook->>Hook: append event to events.jsonl
    Hook->>Cfg: read processing_mode
    alt mode == "background" (opt-in)
        Hook->>Daemon: spawn if dead
        Note over Daemon: 5 min quiet debounce
        Daemon->>Cfg: re-read mode each tick
        Daemon-->>API: spawn summarization
        API-->>Daemon: structured_output
    else mode == "foreground" (default)
        Hook--xDaemon: do NOT spawn
        Note over User: User runs chronicle process
        User->>API: spawn summarization (on demand)
        API-->>User: structured_output
    end
```

### Marker state machine

Every session lives in one of four states. Transitions are driven by summarization outcomes (success / transient error / terminal error) and explicit user commands (`--force`, `--retry-failed`).

```mermaid
stateDiagram-v2
    [*] --> Unprocessed: new JSONL

    Unprocessed --> Success: claude -p ok
    Unprocessed --> Retriable: transient or parse error
    Retriable --> Success: retry ok
    Retriable --> Retriable: another transient
    Retriable --> Terminal: attempts == max_retries

    Success: .processed/&lt;hash&gt; written<br/>+ session .md
    Retriable: .failed/&lt;hash&gt;.json<br/>terminal=false<br/>attempts &lt; max
    Terminal: .failed/&lt;hash&gt;.json<br/>terminal=true

    Terminal --> Unprocessed: chronicle process --retry-failed
    Success --> Unprocessed: chronicle process --force

    state Unprocessed {
        direction LR
        [*] --> infra_error: claude missing / auth failed
        infra_error --> [*]: no retry budget consumed
    }
```

### Error classification

Not every failure is the same. Infra errors (missing binary, auth) are a daemon-level problem, not a session problem — they never charge a session's retry budget.

```mermaid
flowchart LR
    CALL["spawn_claude()"] --> RC{"returncode<br/>== 0 ?"}
    RC -- no --> ERRSNIFF{"auth / command-not-found<br/>stderr hint?"}
    ERRSNIFF -- yes --> INFRA
    ERRSNIFF -- no --> TRANS
    RC -- yes --> PARSE{"outer JSON<br/>parseable ?"}
    PARSE -- no --> PERR["ErrorKind.PARSE<br/>(counts as retry)"]
    PARSE -- yes --> ISERR{"outer.is_error ?"}
    ISERR -- yes --> TRANS["ErrorKind.TRANSIENT<br/>(counts as retry)"]
    ISERR -- no --> OK["Success<br/>.processed/&lt;hash&gt;"]
    INFRA["ErrorKind.INFRA<br/>(does NOT count as retry;<br/>logs once per daemon lifetime)"] --> FIX["User fixes PATH /<br/>auth; next tick retries"]
```

### Claude binary resolution

Daemons spawned by launchd/systemd inherit a minimal PATH. chronicle_cli resolves `claude` once at spawn time using `shutil.which` plus fallback dirs, then reuses the absolute path.

```mermaid
flowchart TB
    NEED["spawn_claude(...)"] --> CACHED{"cached path?"}
    CACHED -- yes --> USE["use cached"]
    CACHED -- no --> WHICH["shutil.which('claude')"]
    WHICH -- found --> CACHE["cache + use"]
    WHICH -- miss --> FB["fallback dirs:<br/>~/.local/bin<br/>/opt/homebrew/bin<br/>/usr/local/bin"]
    FB -- found --> CACHE
    FB -- miss --> MISSING["ClaudeNotFound<br/>→ ErrorKind.INFRA"]
    USE --> EXEC["asyncio.create_subprocess_exec<br/>with sanitized env<br/>(no ANTHROPIC_API_KEY /<br/>AUTH_TOKEN / BASE_URL)"]
    CACHE --> EXEC
```

### Hook event flow (background mode)

In background mode, the daemon watches events.jsonl and processes sessions after quiet time. (In foreground mode, the daemon is absent — only the hook-logging rows fire; summarization waits until you run `chronicle process`.)

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant H as chronicle-hook
    participant EQ as events.jsonl
    participant D as Daemon (bg only)
    participant API as claude -p
    participant FS as ~/.chronicle/

    Note over CC,FS: Session begins
    CC->>H: SessionStart (sync)
    H->>EQ: append event
    H->>H: load past session titles
    H-->>CC: additionalContext (past titles)

    Note over CC,FS: User works; hooks keep logging
    CC->>H: UserPromptSubmit (async)
    H->>EQ: append event
    CC->>H: Stop / SessionEnd (async)
    H->>EQ: append event

    Note over D: (background only)<br/>poll + debounce + scan<br/>until 5 min quiet
    D->>EQ: read new events from offset
    D->>D: acquire processing.lock
    par workers (up to 5)
        D->>API: claude -p --json-schema<br/>(sanitized env)
        API-->>D: structured_output
    end
    D->>FS: write .md + .processed/hash
    Note over D,FS: on error:<br/>INFRA → log only, no retry charge<br/>TRANSIENT/PARSE → .failed/hash.json<br/>at max_retries → terminal=true
    D->>D: release processing.lock
```

### Session processing pipeline

What happens inside each worker when a session is processed:

```mermaid
flowchart TB
    JSONL["~/.claude/projects/&lt;slug&gt;/&lt;session-id&gt;.jsonl"]

    subgraph extract["Extract (extractor.py)"]
        direction TB
        E1["Parse JSONL line by line"]
        E2["Identify user prompts<br/>(filter system-injected content)"]
        E3["Extract tool calls:<br/>Bash, Edit, Write, Read,<br/>Agent, Skill, WebSearch..."]
        E4["Extract tool results<br/>(cap at 10KB each)"]
        E5["Build timeline:<br/>user → assistant → tool_result"]
        E1 --> E2 --> E3 --> E4 --> E5
    end

    subgraph redact["Redact Secrets"]
        direction TB
        R1["Scan for API keys<br/>sk-, ghp_, AKIA, xoxb-"]
        R2["Scan for auth headers<br/>Bearer, Authorization"]
        R3["Scan for private keys<br/>-----BEGIN RSA"]
        R4["Scan for JWTs<br/>eyJ..."]
        R5["Scan for connection URIs<br/>postgres://user:pass@"]
        R6["Scan for env assignments<br/>API_KEY=, SECRET="]
        R7["Full-redact .env, .pem, .key files"]
        R1 & R2 & R3 & R4 & R5 & R6 & R7
    end

    subgraph filter["Filter (filtering.py)"]
        direction TB
        F1{"Self-session?<br/>(prompt starts with<br/>'You are writing a<br/>high-fidelity engineering<br/>chronicle')"}
        F2{"Project in<br/>skip_projects?"}
        F3{"Already<br/>chronicled?"}
        F1 -->|yes| SKIP
        F1 -->|no| F2
        F2 -->|yes| SKIP
        F2 -->|no| F3
        F3 -->|yes| SKIP
        F3 -->|no| PASS
    end

    subgraph summarize["Summarize (summarizer.py)"]
        direction TB
        S1["Build prompt:<br/>recent titles + transcript"]
        S2["Spawn: claude -p<br/>--json-schema CHRONICLE_JSON_SCHEMA<br/>--effort max<br/>--model opus<br/>--fallback-model sonnet"]
        S3["Parse outer JSON wrapper"]
        S4["Extract structured_output<br/>(validated by --json-schema)"]
        S5["Read total_cost_usd"]
        S6["Populate ChronicleEntry:<br/>title, summary, narrative,<br/>decisions, problems_solved,<br/>architecture, planning, ..."]
        S1 --> S2 --> S3 --> S4 --> S5 --> S6
    end

    subgraph write["Write (storage.py)"]
        direction TB
        W1["on success:<br/>write_session_record() → sessions/date_id_title.md<br/>append_to_chronicle() → chronicle.md timeline<br/>rebuild_prompts_section()<br/>mark_succeeded() → .processed/&lt;hash&gt;"]
        W2["on transient/parse error:<br/>record_failed_attempt() → .failed/&lt;hash&gt;.json<br/>terminal=true once attempts reaches max_retries"]
        W3["on infra error (missing claude, auth):<br/>log only, don't charge retry budget"]
    end

    JSONL --> extract
    extract --> redact
    redact --> filter
    PASS --> summarize
    SKIP["Skip"]
    summarize --> write
```

---

## Commands

### `chronicle process`

Explicitly summarize pending sessions. In background mode, pauses the service manager first and holds the processing lock; in foreground mode, just holds the lock.

```mermaid
flowchart TB
    CMD["chronicle process [--project N] [--workers 5]<br/>[--force] [--retry-failed] [--dry-run]"]
    CMD --> MODE{"mode == background ?"}
    MODE -- yes --> PAUSE["service.pause_service()<br/>(launchctl bootout / systemctl stop)"]
    MODE -- no --> SKIPPAUSE["(no daemon to pause)"]
    PAUSE --> LOCK
    SKIPPAUSE --> LOCK
    LOCK["acquire ~/.chronicle/processing.lock<br/>(blocking)"]
    LOCK --> SCAN["scan ~/.claude/projects/*/<br/>extract each JSONL"]
    SCAN --> FILTER{"should_skip?"}
    FILTER -- "already chronicled" --> SKIPSUCC["skip (unless --force)"]
    FILTER -- "terminal failure" --> SKIPFAIL["skip (unless --retry-failed or --force)"]
    FILTER -- "skip-project / self-session" --> SKIPOTH["skip"]
    FILTER -- "None" --> ELIGIBLE["eligible"]
    ELIGIBLE --> PARALLEL["asyncio.Semaphore(workers)<br/>parallel summarize via claude_cli"]
    PARALLEL --> WRITE["write_chronicle() →<br/>.processed/&lt;hash&gt; OR .failed/&lt;hash&gt;.json"]
    WRITE --> RESUME{"paused earlier?"}
    RESUME -- yes --> RESUMESVC["service.resume_service()"]
    RESUME -- no --> DONE["summary: processed / skipped / failed"]
    RESUMESVC --> DONE
```

### `chronicle query`

Browse and search chronicle data:

```mermaid
flowchart TB
    subgraph projects["chronicle query projects"]
        P1["Scan ~/.chronicle/projects/*/sessions/*.md"]
        P2["Count .md files per project"]
        P3["Scan ~/.claude/projects/ for un-chronicled"]
        P4["Print chronicled + pending"]
        P1 --> P2 --> P3 --> P4
    end

    subgraph sessions["chronicle query sessions"]
        S1["Resolve project from cwd"]
        S2["Find chronicle.md"]
        S3["List sessions/*.md with titles"]
        S4["Print paths"]
        S1 --> S2 --> S3 --> S4
    end

    subgraph timeline["chronicle query timeline"]
        T1["Walk all projects/sessions/*.md"]
        T2["Extract date, title, decisions count"]
        T3["Sort by date, newest first"]
        T4["Print top N"]
        T1 --> T2 --> T3 --> T4
    end

    subgraph search["chronicle query search 'term'"]
        SR1["Glob all .md files in projects/"]
        SR2["Regex match with context"]
        SR3["Print matches with highlighting"]
        SR1 --> SR2 --> SR3
    end
```

### `chronicle rewind`

Navigate session history with multiple views:

```mermaid
flowchart TB
    CMD["chronicle rewind"]

    CMD --> LOAD["Load all sessions/*.md<br/>number them 1..N chronologically"]

    LOAD --> MODE{"Which mode?"}

    MODE -->|"rewind"| LIST["Show numbered list:<br/>#  Date  Turns  Dec  Title"]
    MODE -->|"rewind N"| SHOW["Show session #N:<br/>date, summary, decisions,<br/>open questions, files"]
    MODE -->|"rewind --since N"| SINCE["Show sessions #N through latest:<br/>condensed with decisions + open ?s"]
    MODE -->|"rewind --diff N"| DIFF["Compare #N vs cumulative prior:<br/>NEW decisions, NEW files,<br/>RESOLVED questions"]
    MODE -->|"rewind --summary N"| SUMMARY["Send sessions #N+ to claude -p<br/>--effort low --fallback-model sonnet<br/>Print AI narrative"]
    MODE -->|"rewind --delete N"| DELETE["Remove session .md<br/>Remove from chronicle.md<br/>Remove .processed marker"]
    MODE -->|"rewind --prune"| PRUNE["Find sessions with 0 decisions<br/>Confirm each, then delete"]
```

### `chronicle insight`

Generate an LLM-powered HTML dashboard:

```mermaid
flowchart TB
    CMD["chronicle insight [project-name]"]

    CMD --> FIND["Substring match against project slugs<br/>→ ~/.chronicle/projects/&lt;project-slug&gt;/"]
    FIND --> PARSE["Parse all sessions/*.md:<br/>title, date, turns, cost,<br/>decisions, open_questions,<br/>files_changed, stack, summary"]
    PARSE --> AGG["Aggregate into JSON payload:<br/>totals, file_counts, stack_counts,<br/>all_decisions, all_questions"]
    AGG --> PROMPT["Build prompt:<br/>INSIGHT_PROMPT + JSON payload"]
    PROMPT --> CLAUDE["claude -p --effort max<br/>--fallback-model sonnet<br/>--no-session-persistence"]
    CLAUDE --> HTML["Extract HTML from response"]
    HTML --> WRITE["Write insight.html"]
    WRITE --> OPEN["webbrowser.open(insight.html)"]
```

### `chronicle story`

Generate a unified project narrative:

```mermaid
flowchart TB
    CMD["chronicle story [project-name]"]

    CMD --> FIND["Substring match against project slugs<br/>→ ~/.chronicle/projects/&lt;project-slug&gt;/"]
    FIND --> LOAD["Load ALL sessions/*.md<br/>full content, chronologically"]
    LOAD --> STRIP["Strip turn-by-turn logs<br/>and verbatim prompts<br/>(too noisy for synthesis)"]
    STRIP --> BUILD["Concatenate as:<br/>=== SESSION: file1 ===<br/>content<br/>=== SESSION: file2 ===<br/>content"]
    BUILD --> TRUNC{"Total > 400K chars?"}
    TRUNC -->|yes| CUT["Truncate with [... truncated ...]"]
    TRUNC -->|no| FULL["Use full content"]
    CUT & FULL --> PROMPT["STORY_PROMPT:<br/>'Write chronological project journal.<br/>Group by phase, not session.<br/>Preserve exact technical details.<br/>Write like a senior engineer.'"]
    PROMPT --> CLAUDE["claude -p --effort max<br/>--fallback-model sonnet<br/>timeout 600s"]
    CLAUDE --> MD["Write story.md"]
    MD --> PRINT["Print path + cost"]
```

### `chronicle daemon`

Background daemon management:

```mermaid
flowchart TB
    subgraph start["chronicle daemon --bg"]
        direction TB
        BG1["os.fork()"]
        BG2["os.setsid() — detach from terminal"]
        BG3["Redirect stdout/stderr → daemon.log"]
        BG4["Acquire fcntl.flock on daemon.pid"]
        BG5["Write PID to daemon.pid"]
        BG6["Enter async event loop"]
        BG1 --> BG2 --> BG3 --> BG4 --> BG5 --> BG6
    end

    subgraph status["chronicle daemon --status"]
        direction TB
        ST1["Read PID from daemon.pid"]
        ST2["os.kill(pid, 0) — check alive"]
        ST3["Print: running (pid N) or not running"]
        ST1 --> ST2 --> ST3
    end

    subgraph stop["chronicle daemon --stop"]
        direction TB
        SP1["Read PID from daemon.pid"]
        SP2["Send SIGTERM"]
        SP3["Daemon catches via loop.add_signal_handler"]
        SP4["Sets stop_event → loop exits gracefully"]
        SP1 --> SP2 --> SP3 --> SP4
    end
```

---

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
# Foreground mode (default — zero passive token burn)
curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash

# Background mode (daemon auto-summarizes)
CHRONICLE_MODE=background curl -fsSL https://raw.githubusercontent.com/ehzawad/claudetalktoclaude/main/install.sh | bash
```

The script checks your platform, finds Python 3.10+, clones to `~/.chronicle/src`, creates a venv, configures hooks in `~/.claude/settings.json`, and sets secure permissions. Restart Claude Code to activate.

You can switch modes at any time with `chronicle install-daemon` / `chronicle uninstall-daemon`. Check current state with `chronicle doctor`.

To update, re-run the install command. It handles dirty install directories automatically.

### Ubuntu 24.04 LTS note

If you use background mode on Ubuntu, enable user-service persistence so the systemd daemon keeps running after logout:

```bash
sudo loginctl enable-linger "$USER"
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
chronicle query projects                # see what sessions exist
chronicle process --workers 5           # process all past sessions
chronicle process --project myproject   # substring match on slug
chronicle process --dry-run             # preview without processing
```

## Usage

After setup, everything is automatic. These commands are for browsing, analysis, and manual processing:

```bash
# Browse
chronicle query sessions              # current project's chronicle.md
chronicle query projects              # all projects
chronicle query timeline              # recent sessions
chronicle query search "auth"         # full-text search

# Rewind — navigate session history
chronicle rewind                      # numbered session list
chronicle rewind 3                    # view session #3
chronicle rewind --since 2            # sessions #2 through latest
chronicle rewind --diff 3             # what was NEW in session #3
chronicle rewind --summary 2          # AI-summarize from #2 onward
chronicle rewind --delete 3           # delete session #3
chronicle rewind --prune              # delete all sessions with 0 decisions

# Insight — per-project analysis
chronicle insight                     # HTML dashboard for current directory
chronicle insight sql                 # substring match on slug

# Story — unified project narrative
chronicle story                       # story.md for current directory
chronicle story whisper               # substring match on slug

# Process
chronicle process --workers 5                  # summarize pending sessions
chronicle process --project sql                # substring match on slug
chronicle process --force --workers 5          # reprocess successful sessions
chronicle process --retry-failed --workers 5   # retry terminal-failure sessions
chronicle process --dry-run                    # preview only

# Mode management
chronicle doctor                      # diagnose: mode, claude path, drift, counts
chronicle install-daemon              # switch to background mode (starts daemon)
chronicle uninstall-daemon            # switch to foreground mode (stops daemon)

# Daemon internals (background mode only)
chronicle daemon --status
chronicle daemon --stop

# Maintenance
chronicle --version
chronicle reload                      # reinstall + restart daemon (if running)
```

All project names are substring matches against slugs (see [How project names work](#how-project-names-work-in-commands)). Run from anywhere.

## Output formats

Each project gets up to three views:

| Output | What it is | How to access |
|--------|-----------|---------------|
| **chronicle.md** | Cumulative session records — auto-generated as sessions are processed | `chronicle query sessions` |
| **insight.html** | LLM-generated HTML dashboard with charts, badges, and narrative | `chronicle insight [project-name]` |
| **story.md** | Unified chronological project narrative for stakeholders | `chronicle story [project-name]` |

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
| **Cost** | Per-session summarization cost tracked automatically |

## Configuration

`~/.chronicle/config.json` (auto-created):

| Key | Default | Description |
|-----|---------|-------------|
| `processing_mode` | `"foreground"` | `"foreground"` (no daemon) or `"background"` (daemon auto-processes). Set via `chronicle install-daemon` / `uninstall-daemon`. |
| `model` | `"opus"` | Model for summarization |
| `fallback_model` | `"sonnet"` | Auto-fallback when primary model is overloaded |
| `concurrency` | `5` | Parallel workers |
| `poll_interval_seconds` | `5` | Daemon poll interval (background mode) |
| `quiet_minutes` | `5` | Global debounce — minutes of silence before daemon processes |
| `scan_interval_minutes` | `30` | How often daemon scans for un-evented sessions |
| `max_retries` | `3` | Give up after N transient-failure attempts (marks `.failed` terminal) |
| `skip_projects` | `[]` | Project slugs to exclude |

## Where things live

```
~/.claude/projects/<slug>/*.jsonl     <- session data (Claude Code writes these)
~/.chronicle/
  ├── events.jsonl                    <- hook event queue
  ├── events.offset                   <- daemon read position (bytes)
  ├── config.json                     <- configuration (including processing_mode)
  ├── daemon.pid                      <- singleton lock (fcntl flock, bg mode)
  ├── daemon.log                      <- daemon stdout/stderr
  ├── processing.lock                 <- mutex between daemon and `chronicle process`
  ├── .processed/<hash>               <- success markers (session .md exists)
  ├── .failed/<hash>.json             <- failure records: {attempts, terminal, error_kind}
  └── projects/<slug>/
      ├── chronicle.md                <- cumulative project log
      ├── insight.html                <- HTML dashboard (chronicle insight)
      ├── story.md                    <- unified narrative (chronicle story)
      └── sessions/
          └── 2026-04-01_0611_abc12345_wiring-hooks.md
```

The `<slug>` is your project path with `/` replaced by `-`.

## Troubleshooting

Run `chronicle doctor` for a diagnostic report:

- **`claude` not found** — the daemon can't summarize. Install Claude Code CLI or ensure it's on the daemon's PATH. In background mode, `chronicle install-daemon` bakes PATH into the launchd plist / systemd unit so launchd's minimal env (`/usr/bin:/bin:/usr/sbin:/sbin`) doesn't cause `FileNotFoundError`.
- **Mode drift warnings** — config says one mode but the service file / daemon says another. Fix with `chronicle install-daemon` or `chronicle uninstall-daemon` as indicated.
- **Sessions in terminal-failure state** — `.failed/<hash>.json` with `terminal=true`. After fixing the underlying issue, run `chronicle process --retry-failed --workers 5`.

## Security

**Secret redaction** — tool outputs are scanned for known patterns before storage. API keys, auth headers, private keys, JWTs, connection strings, and env var assignments are replaced with `[REDACTED]`. Sensitive file types (`.env`, `.pem`, `.key`) get fully redacted content. User prompts are not redacted.

**Subscription routing** — `claude -p` subprocess calls strip `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` from the environment so summarization always routes through your paid subscription instead of API credits or a proxy gateway ([anthropics/claude-code#2051](https://github.com/anthropics/claude-code/issues/2051)).

**File permissions** — `~/.chronicle/` is `0700` (owner-only), matching `~/.claude/`.

**Singleton daemon** — PID file with `fcntl.flock`. Inode validation detects stale locks from deleted/recreated PID files.

**Observer only** — chronicle never writes to `~/.claude/`, never blocks hooks, never modifies Claude Code behavior. The only sync hook (SessionStart) injects past decision titles as additive context. All data lives in `~/.chronicle/` — deleting sessions (`--delete`, `--prune`) only removes chronicle's own markdown files, never Claude Code's session data. You can prune everything and re-run `chronicle process` to regenerate from the original JSONL files.

## How is this possible

Two things Claude Code already provides:

1. **Session JSONL files** at `~/.claude/projects/<slug>/*.jsonl` — every conversation turn. We just read them.
2. **Hooks** in `~/.claude/settings.json` — lifecycle events. We just listen.

## Project structure

```
chronicle/
  hook.py             # event logging, daemon spawn, context injection
  daemon.py           # polling, global debounce, session scan, parallel dispatch
  extractor.py        # JSONL parsing, secret redaction, timeline building
  summarizer.py       # claude -p --json-schema, structured output, cost tracking
  storage.py          # atomic writes, dedup, retry tracking, chronicle.md management
  filtering.py        # session skip logic (self-detection, project exclusion)
  batch.py            # retroactive bulk processing (chronicle process)
  query.py            # search, timeline, project listing
  rewind.py           # numbered session navigator with --diff, --since, --summary
  insight.py          # LLM-generated HTML dashboard per project
  story.py            # LLM-generated unified project narrative
  config.py           # paths, defaults, permissions
  install_hooks.py    # idempotent hook configuration
  __main__.py         # CLI dispatcher + install-daemon + reload
```
