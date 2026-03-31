# Decision Chronicle

Captures planning decisions, architecture choices, debugging context, and implementation rationale from coding sessions — writes them to searchable markdown files automatically.

It records both minds: the programmer's intuitions, pushback, and "wait, what about X?" moments, and the assistant's analysis, trade-off evaluations, and course corrections. Six months later, anyone reading the chronicle doesn't just see what was built — they see how it was thought through.

## The problem

You spend time planning: architecture decisions, stack choices, testing strategies. Then you delegate implementation, guiding it step by step. Everything lands in git. But the *reasoning* — trade-offs discussed, approaches rejected, the "why" behind the "what" — lives only in ephemeral chat sessions. After they end, that knowledge is gone.

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

Nothing changes about your workflow. Work as usual, close the session.

1. **Hooks fire** on every prompt, response, and session end — logging events to `events.jsonl`
2. **A background daemon** runs continuously (see [Global debounce](#global-debounce) and [Parallel workers](#parallel-workers) below for how)
3. **Processing** means: extract the session JSONL, redact secrets, send to `claude -p` with `--json-schema` for validated structured output (`--effort max`, `--fallback-model sonnet`, uses your subscription)
4. **Output**: one `chronicle.md` per project (cumulative, chronological, with timeline table) plus individual session `.md` files
5. **Next session** gets past decision titles injected as context automatically via the SessionStart hook
6. **On-demand**: `chronicle insight` generates an HTML dashboard, `chronicle story` generates a unified narrative — both call `claude -p` with the aggregated session data

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

### System overview

How all the pieces connect — from Claude Code sessions to markdown output:

```mermaid
flowchart TB
    subgraph cc["Claude Code Sessions"]
        S1["Session: Project A"]
        S2["Session: Project B"]
        S3["Session: Project C"]
    end

    subgraph hooks["Hook Layer (hook.py)"]
        direction TB
        SS["SessionStart<br/>(sync)"]
        UP["UserPromptSubmit<br/>(async)"]
        ST["Stop / SessionEnd<br/>(async)"]
        EQ[("~/.chronicle/<br/>events.jsonl")]
    end

    subgraph daemon["Background Daemon (daemon.py)"]
        direction TB
        PL["Poll events.jsonl<br/>every 5 seconds"]
        DE["Global Debounce<br/>5 min quiet across ALL projects"]
        SC["Periodic Scanner<br/>every 30 min scan ~/.claude/projects/"]
        PD{{"Pending<br/>sessions"}}
        SEM["Semaphore<br/>5 parallel workers"]
    end

    subgraph pipeline["Processing Pipeline"]
        direction TB
        EX["Extract JSONL<br/>(extractor.py)"]
        FI["Filter<br/>(filtering.py)"]
        RD["Redact Secrets<br/>(extractor.py)"]
        SM["claude -p<br/>--json-schema --effort max<br/>--fallback-model sonnet<br/>(summarizer.py)"]
    end

    subgraph output["Output (storage.py)"]
        direction TB
        SF["Per-session .md"]
        CM["chronicle.md<br/>(cumulative)"]
        MK[".processed/<br/>marker + cost"]
    end

    S1 & S2 & S3 --> SS & UP & ST
    SS -->|"spawn daemon<br/>if not running"| PL
    SS -->|"inject past<br/>decisions"| S1 & S2 & S3
    SS & UP & ST --> EQ
    EQ --> PL
    PL --> DE
    UP -->|"reset debounce<br/>timer"| DE
    SC -->|"find sessions<br/>without events"| PD
    DE -->|"all quiet<br/>for 5 min"| PD
    PD --> SEM
    SEM --> EX --> FI --> RD --> SM
    SM -->|"structured_output<br/>+ total_cost_usd"| SF & CM & MK
```

### Daemon lifecycle

What the daemon does from startup to processing:

```mermaid
stateDiagram-v2
    [*] --> Starting: chronicle daemon --bg<br/>or hook spawns it

    Starting --> AcquireLock: PID file + fcntl flock
    AcquireLock --> Running: lock acquired
    AcquireLock --> [*]: another daemon owns lock

    Running --> Polling: every 5 seconds

    state Polling {
        [*] --> ReadEvents: read events.jsonl<br/>from byte offset
        ReadEvents --> CategorizeEvents
        CategorizeEvents --> CheckDebounce
        CheckDebounce --> ScanCheck: check if 30min<br/>since last scan

        state ScanCheck {
            [*] --> ScanNeeded: time elapsed >= 30min
            ScanNeeded --> ScanProjects: walk ~/.claude/projects/
            ScanProjects --> QueueUnprocessed: add sessions<br/>without events
            [*] --> ScanSkipped: too soon
        }

        ScanCheck --> WaitOrProcess

        state WaitOrProcess {
            [*] --> StillActive: UserPromptSubmit<br/>seen recently
            StillActive --> ResetTimer: remove session<br/>from pending
            [*] --> AllQuiet: 5 min silence<br/>across ALL projects
            AllQuiet --> ProcessBatch: dispatch to<br/>parallel workers
        }
    }

    Polling --> ValidateLock: check inode<br/>matches PID file
    ValidateLock --> Polling: still valid
    ValidateLock --> [*]: PID file replaced<br/>by another daemon

    Running --> [*]: SIGTERM / SIGINT / SIGHUP
```

### Hook event flow

What happens at each lifecycle event in a Claude Code session:

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant H as chronicle-hook
    participant EQ as events.jsonl
    participant D as Daemon
    participant API as claude -p
    participant FS as Markdown Files

    Note over CC,FS: Session begins

    CC->>H: SessionStart (sync)
    H->>EQ: append {session_id, transcript_path, cwd}
    H->>H: check daemon PID file
    alt daemon not running
        H->>D: subprocess.Popen(chronicle.daemon)
    end
    H->>H: load recent titles from sessions/*.md
    H-->>CC: {"additionalContext": "Previous sessions:\\n- title1\\n- title2"}

    Note over CC,FS: User works normally

    CC->>H: UserPromptSubmit (async)
    H->>EQ: append {session_id, prompt}
    Note over D: debounce timer resets<br/>removes session from pending

    CC->>H: UserPromptSubmit (async)
    H->>EQ: append event
    Note over D: debounce resets again

    CC->>H: Stop (async)
    H->>EQ: append {session_id, transcript_path}
    Note over D: session added to pending

    CC->>H: SessionEnd (async)
    H->>EQ: append {session_id, reason}

    Note over D: 5 minutes of silence...

    D->>EQ: read new events from offset
    D->>D: categorize: Stop/SessionEnd → pending
    D->>D: all quiet? yes → process batch

    par parallel workers (up to 5)
        D->>D: extract_session(transcript.jsonl)
        D->>D: redact secrets from tool outputs
        D->>API: stdin: summarization prompt + transcript
        Note over API: claude -p --json-schema<br/>--effort max<br/>--fallback-model sonnet<br/>--no-session-persistence<br/>env: ANTHROPIC_API_KEY stripped
        API-->>D: {"structured_output": {...}, "total_cost_usd": 0.15}
    end

    D->>FS: write session .md (per-session record)
    D->>FS: append to chronicle.md (timeline + detail)
    D->>FS: mark .processed/{hash} (with cost)
    D->>D: save events.jsonl offset
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
        W1["write_session_record()<br/>→ sessions/date_id_title.md"]
        W2["append_to_chronicle()<br/>→ chronicle.md timeline + detail"]
        W3["rebuild_prompts_section()<br/>→ chronological prompts at end"]
        W4["mark_chronicled()<br/>→ .processed/{hash} with cost"]
        W1 --> W2 --> W3 --> W4
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

Manually trigger processing for all or specific projects:

```mermaid
flowchart TB
    CMD["chronicle process<br/>--project [name] --workers 5 --force"]

    CMD --> STOP["Stop running daemon<br/>(wait up to 30s, SIGKILL if stuck)"]
    STOP --> SCAN["Scan ~/.claude/projects/*/<br/>Substring match against slugs<br/>→ find *.jsonl session files"]
    SCAN --> FILTER{"For each session:<br/>--force?"}
    FILTER -->|"--force"| ELIGIBLE["Add to eligible"]
    FILTER -->|"no --force"| CHECK{"already_chronicled?<br/>self-session?<br/>skip_projects?"}
    CHECK -->|skip| SKIP["Skip"]
    CHECK -->|eligible| ELIGIBLE

    ELIGIBLE --> PARALLEL["Process in parallel<br/>(N workers via asyncio.Semaphore)"]

    subgraph worker["Each worker"]
        direction TB
        WE["extract_session()"]
        WS["async_summarize_session()"]
        WW["write_session_record()"]
        WC["append_to_chronicle()"]
        WE --> WS --> WW --> WC
    end

    PARALLEL --> worker
    worker --> RESTART["Restart daemon"]
    RESTART --> SUMMARY["Print summary:<br/>Processed: N<br/>Skipped: N<br/>Already done: N"]
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
chronicle process --workers 5           # all projects
chronicle process --project sql         # substring match on slug
chronicle process --force --workers 5   # reprocess everything
chronicle process --dry-run             # preview only

# Daemon
chronicle daemon --status
chronicle daemon --stop
chronicle install-daemon              # auto-start on login

# Maintenance
chronicle --version
chronicle reload                      # reinstall + restart daemon
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
| `model` | `"opus"` | Model for summarization |
| `fallback_model` | `"sonnet"` | Auto-fallback when primary model is overloaded |
| `concurrency` | `5` | Parallel workers |
| `poll_interval_seconds` | `5` | Daemon poll interval |
| `quiet_minutes` | `5` | Global debounce — minutes of silence before processing |
| `scan_interval_minutes` | `30` | How often daemon scans for un-evented sessions |
| `max_retries` | `3` | Give up after N failed summarization attempts |
| `skip_projects` | `[]` | Project slugs to exclude |

## Where things live

```
~/.claude/projects/<slug>/*.jsonl     <- session data (Claude Code writes these)
~/.chronicle/
  ├── events.jsonl                    <- hook event queue
  ├── events.offset                   <- daemon read position (bytes)
  ├── config.json                     <- configuration
  ├── daemon.pid                      <- singleton lock (fcntl flock)
  ├── daemon.log                      <- daemon stdout/stderr
  ├── .processed/                     <- dedup markers (hash → session_id + cost)
  └── projects/<slug>/
      ├── chronicle.md                <- cumulative project log
      ├── insight.html                <- HTML dashboard (chronicle insight)
      ├── story.md                    <- unified narrative (chronicle story)
      └── sessions/
          └── 2026-04-01_0611_abc12345_wiring-hooks.md
```

The `<slug>` is your project path with `/` replaced by `-`.

## Security

**Secret redaction** — tool outputs are scanned for known patterns before storage. API keys, auth headers, private keys, JWTs, connection strings, and env var assignments are replaced with `[REDACTED]`. Sensitive file types (`.env`, `.pem`, `.key`) get fully redacted content. User prompts are not redacted.

**Subscription routing** — `claude -p` subprocess calls strip `ANTHROPIC_API_KEY` from the environment so summarization always routes through your paid subscription instead of API credits ([anthropics/claude-code#2051](https://github.com/anthropics/claude-code/issues/2051)).

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
