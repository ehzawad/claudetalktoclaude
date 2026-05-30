"""Parse Claude Code session JSONL files and extract meaningful content.

The JSONL files at ~/.claude/projects/<slug>/<session-id>.jsonl contain every
message exchanged in a session. This module extracts structured content,
preserving chronological order, for high-fidelity summarization. Unknown
top-level message types, content blocks, and tool names are captured
generically so new Claude Code features are never silently dropped.

Two output formats, both uncapped (the whole session is kept) with secrets
masked in commands, tool inputs, and tool outputs:
- digest_to_text():  interleaved timeline with one-liner tool indexes plus
  full redacted tool inputs, fed to claude -p for summarization.
- timeline_to_log(): full chronological log for the archival session markdown.
"""

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UserPrompt:
    text: str
    timestamp: str
    uuid: str


@dataclass
class ToolDetail:
    """Rich detail about a tool call for the archival record.

    input stores the full redacted tool input dump. The other fields are
    structured conveniences for readable one-line indexes and focused blocks.
    """
    tool: str
    summary: str  # one-liner for the LLM prompt
    input: str = ""  # full redacted tool input
    path: str = ""
    command: str = ""
    content: str = ""  # Write content or Edit new_string
    old_content: str = ""  # Edit old_string
    query: str = ""  # WebSearch, Grep, Glob
    description: str = ""  # Agent


@dataclass
class TimelineEntry:
    """A single turn in the chronological conversation timeline."""
    role: str  # "user", "assistant", "tool_result", or future transcript type
    timestamp: str
    text: str  # main text content
    tool_actions: list[str] = field(default_factory=list)  # one-liners for LLM
    tool_details: list[ToolDetail] = field(default_factory=list)  # full detail for log
    tool_results: list[str] = field(default_factory=list)


@dataclass
class SessionDigest:
    session_id: str
    project_path: str
    project_slug: str
    start_time: str
    end_time: str
    git_branch: str
    user_prompts: list[UserPrompt] = field(default_factory=list)
    assistant_responses: list[str] = field(default_factory=list)
    tool_actions: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    total_turns: int = 0


def _session_id_from_jsonl(jsonl_path: str | Path) -> str:
    """Return the canonical sessionId stored inside a Claude JSONL.

    Claude's current filenames match `sessionId`, but marker identity is based
    on the extractor's `digest.session_id`. Keep scanner/doctor probes aligned
    with extraction by using the last non-meta in-file sessionId, falling back
    to the filename stem when the file is unreadable or lacks sessionId fields.
    """
    path = Path(jsonl_path)
    session_id = path.stem
    try:
        with open(path, errors="replace") as f:  # tolerate corrupt/binary jsonl (BUG-20)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "file-history-snapshot":
                    continue
                if entry.get("isMeta"):
                    continue
                sid = entry.get("sessionId", "")
                if sid:
                    session_id = sid
    except OSError:
        return path.stem
    return session_id


# Secret patterns to redact from tool outputs, commands, and file content.
# Order matters — earlier alternations take precedence at each match position.
# Header-style alternations (Authorization, Cookie, Proxy-Authorization,
# X-*-Key) consume the whole header VALUE to end of line so no token after
# them is left naked.
_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----|"
    r"Authorization:\s*[^\r\n]+|"
    r"Proxy-Authorization:\s*[^\r\n]+|"
    r"Cookie:\s*[^\r\n]+|"
    r"Set-Cookie:\s*[^\r\n]+|"
    r"X-[A-Za-z-]+-(?:Key|Token|Auth|Secret):\s*[^\r\n]+|"
    # JSON-quoted credentials: "access_token": "ya29...", "SecretAccessKey": "..."
    # — a quoted key, colon, quoted value. Narrow on purpose (quoted value only)
    # so it cannot eat numeric/unquoted neighbours like {"token": 5, "next": "x"}.
    r'"(?:api[_-]?key|secret[_-]?access[_-]?key|secret[_-]?key|secret|access[_-]?token|access[_-]?key|refresh[_-]?token|client[_-]?secret|credentials|password|token)"\s*:\s*"[^"]+"|'
    r"(?:export\s+)?(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS|AUTH|PRIVATE_KEY|ACCESS_KEY)"
    r"[_A-Z]*[\s]*[=:]\s*\S+|"
    r"Bearer\s+\S+|"
    r"(?:sk-|pk-|ghp_|gho_|github_pat_|xoxb-|xoxp-|xoxa-|xapp-|xoxr-|"
    r"sk_live_|sk_test_|rk_live_|rk_test_|whsec_|npm_|AKIA)\S+|"
    r"AIza[0-9A-Za-z_-]{35}|"
    r"(?:mongodb\+srv|postgres(?:ql)?|mysql|redis|amqp)://\S+|"
    r"(?:eyJ[A-Za-z0-9_-]{20,}\.){1,2}[A-Za-z0-9_-]+|"  # JWTs
    # URL query-string credentials: ?token=..., &api_key=..., ?access_token=...
    r"[?&](?:token|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
    r"secret[_-]?key|client[_-]?secret|sig|signature)=[^&\s#]+"
    r")",
    re.IGNORECASE,
)

# Sensitive file paths — redact Write content for these
_SENSITIVE_PATHS = re.compile(
    r"\.env|credentials|secret|\.pem|\.key|id_rsa|id_ed25519|\.aws/|\.docker/config",
    re.IGNORECASE,
)

_SENSITIVE_INPUT_KEYS = re.compile(
    r"api[_-]?key|secret|token|password|credential|auth|private[_-]?key|access[_-]?key",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]."""
    if not text:
        return text
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


def _sensitive_file_placeholder(path: str) -> str:
    """Placeholder for content whose path is sensitive even without a token hit."""
    return f"[REDACTED — sensitive file: {Path(path).name}]"


def _redacted_tool_input(inp: dict) -> str:
    """Return a full redacted dump of a tool input dictionary.

    Full input archival is separate from the compact one-line summary. For
    sensitive paths, redact payload-bearing fields before the broader pattern
    scanner runs so removing length caps cannot reveal content that the old cap
    merely happened not to reach.
    """
    safe_inp = inp
    path = str(inp.get("file_path") or inp.get("notebook_path") or "")
    if path and _SENSITIVE_PATHS.search(path):
        safe_inp = dict(inp)
        placeholder = _sensitive_file_placeholder(path)
        for key in ("content", "old_string", "new_string", "new_source"):
            if key in safe_inp:
                safe_inp[key] = placeholder
        if "edits" in safe_inp:
            safe_inp["edits"] = placeholder
    try:
        dumped = json.dumps(safe_inp, ensure_ascii=False)
    except (TypeError, ValueError):
        dumped = str(safe_inp)
    return _redact_secrets(dumped)


def _redact_input_value_for_key(key: str, value) -> str:
    if _SENSITIVE_INPUT_KEYS.search(str(key)):
        return "[REDACTED]"
    return _redact_secrets(str(value))


def _index_snippet(text: str, limit: int = 120) -> str:
    """Compact text for one-line indexes only.

    The archival copy is kept separately in ToolDetail.input/content fields and
    rendered in fenced details blocks.
    """
    one_line = " ".join(str(text).split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:max(0, limit - 3)].rstrip() + "..."


# Patterns to skip in user messages (system-injected content)
_SKIP_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<system-reminder>",
    "<task-notification>",
    "[Request interrupted by user]",
)

# Only strip known system-injected XML tags, not arbitrary angle-bracket content.
# The old re.sub(r"<[^>]+>", "", text) stripped user-typed HTML/XML too.
_SYSTEM_TAG_PATTERN = re.compile(
    r"</?(?:local-command-caveat|local-command-stdout|local-command-stderr|"
    r"command-name|command-message|command-args|system-reminder|"
    r"task-notification|user-prompt-submit-hook)[^>]*>",
    re.DOTALL,
)


def _is_real_user_prompt(content: str) -> bool:
    """Check if a user message is a real prompt (not system-injected)."""
    stripped = content.strip()
    if not stripped:
        return False
    for prefix in _SKIP_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return True


def _extract_text_from_content(content) -> str | None:
    """Extract text from message content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts) if texts else None
    return None


def _extract_tool(block: dict) -> tuple[str | None, ToolDetail | None]:
    """Extract both a one-liner summary and full detail from a tool_use block.

    Returns (summary_string, ToolDetail) or (None, None).
    """
    if block.get("type") != "tool_use":
        return None, None

    name = block.get("name", "unknown")
    inp = block.get("input", {})
    if not isinstance(inp, dict):
        inp = {}

    # Canonicalize casing so a renamed/lowercased builtin (e.g. the observed
    # lowercase 'bash') keeps its dedicated rendering + redaction path instead
    # of degrading to the generic dump.
    _CANON = {
        "bash": "Bash", "edit": "Edit", "write": "Write", "read": "Read",
        "glob": "Glob", "grep": "Grep", "agent": "Agent", "skill": "Skill",
        "websearch": "WebSearch", "webfetch": "WebFetch", "multiedit": "MultiEdit",
    }
    if name in _CANON:
        name = _CANON[name]

    full_input = _redacted_tool_input(inp)

    if name == "Bash":
        cmd = _redact_secrets(inp.get("command", ""))
        summary = f"Bash: {_index_snippet(cmd, 120)}"
        return summary, ToolDetail(
            tool="Bash", summary=summary, input=full_input, command=cmd)

    elif name == "Edit":
        path = inp.get("file_path", "")
        if _SENSITIVE_PATHS.search(path):
            old = _sensitive_file_placeholder(path)
            new = _sensitive_file_placeholder(path)
        else:
            old = _redact_secrets(inp.get("old_string", ""))
            new = _redact_secrets(inp.get("new_string", ""))
        return f"Edit: {path}", ToolDetail(
            tool="Edit", summary=f"Edit: {path}", input=full_input, path=path,
            old_content=old, content=new)

    elif name == "Write":
        path = inp.get("file_path", "")
        content = inp.get("content", "")
        # Fully redact content for sensitive file types
        if _SENSITIVE_PATHS.search(path):
            content = _sensitive_file_placeholder(path)
        else:
            content = _redact_secrets(content)
        return f"Write: {path}", ToolDetail(
            tool="Write", summary=f"Write: {path}", input=full_input,
            path=path, content=content)

    elif name == "Read":
        path = inp.get("file_path", "")
        return f"Read: {path}", ToolDetail(
            tool="Read", summary=f"Read: {path}", input=full_input, path=path)

    elif name in ("Glob", "Grep"):
        pattern = _redact_secrets(inp.get("pattern", ""))
        return f"{name}: {pattern}", ToolDetail(
            tool=name, summary=f"{name}: {pattern}", input=full_input,
            query=pattern)

    elif name == "Agent":
        desc = _redact_secrets(inp.get("description", ""))
        prompt = _redact_secrets(inp.get("prompt", ""))
        return f"Agent: {desc}", ToolDetail(
            tool="Agent", summary=f"Agent: {desc}", input=full_input,
            description=desc, content=prompt)

    elif name == "Skill":
        skill = inp.get("skill", "")
        return f"Skill: {skill}", ToolDetail(
            tool="Skill", summary=f"Skill: {skill}", input=full_input,
            query=skill)

    elif name == "WebSearch":
        query = _redact_secrets(inp.get("query", ""))
        return f"WebSearch: {query}", ToolDetail(
            tool="WebSearch", summary=f"WebSearch: {query}", input=full_input,
            query=query)

    elif name == "WebFetch":
        url = _redact_secrets(inp.get("url", ""))
        return f"WebFetch: {url}", ToolDetail(
            tool="WebFetch", summary=f"WebFetch: {url}", input=full_input,
            query=url)

    elif name == "MultiEdit":
        path = inp.get("file_path", "")
        edits = inp.get("edits", [])
        count = len(edits)
        return f"MultiEdit: {path} ({count} regions)", ToolDetail(
            tool="MultiEdit", summary=f"MultiEdit: {path} ({count} regions)",
            input=full_input, path=path, content=f"{count} edit regions")

    elif name in ("TaskCreate", "TaskUpdate", "TaskList", "TaskStop"):
        # Agent Teams / task tracking (first-class CLI feature). Surface the
        # task subject + status transition so the summarizer can chronicle
        # coordination, not emit a content-free bullet.
        subject = _redact_secrets(str(
            inp.get("subject") or inp.get("description") or inp.get("activeForm") or ""))
        task_id = str(inp.get("taskId") or inp.get("id") or "")
        status = str(inp.get("status") or "")
        bits = [b for b in (task_id, status) if b]
        head = _index_snippet(subject, 80) if subject else (" ".join(bits) if bits else "")
        tail = f" -> {status}" if status and subject else ""
        summary = f"{name}: {head}{tail}".rstrip(": ").rstrip()
        return summary, ToolDetail(
            tool=name, summary=summary, input=full_input,
            description=subject, content=full_input)

    elif name == "Workflow":
        # Dynamic workflows. Capture the workflow name / first script line.
        wf = _redact_secrets(str(
            inp.get("name") or inp.get("workflow") or inp.get("script") or ""))
        summary = f"Workflow: {_index_snippet(wf, 100)}".rstrip(": ").rstrip()
        return summary, ToolDetail(
            tool="Workflow", summary=summary, input=full_input,
            content=full_input)

    elif name == "AskUserQuestion":
        # Interactive question. Flatten the question text(s).
        questions = inp.get("questions")
        qtext = ""
        if isinstance(questions, list) and questions:
            first = questions[0]
            qtext = first.get("question", "") if isinstance(first, dict) else str(first)
        qtext = _redact_secrets(qtext or str(inp.get("question") or ""))
        summary = f"AskUserQuestion: {_index_snippet(qtext, 100)}".rstrip(": ").rstrip()
        return summary, ToolDetail(
            tool="AskUserQuestion", summary=summary, input=full_input,
            content=full_input)

    elif name == "NotebookEdit":
        path = inp.get("notebook_path", "") or inp.get("file_path", "")
        cell = inp.get("cell_type", "")
        suffix = f" ({cell})" if cell else ""
        summary = f"NotebookEdit: {path}{suffix}"
        content = str(inp.get("new_source") or inp.get("content") or "")
        if _SENSITIVE_PATHS.search(path):
            content = _sensitive_file_placeholder(path)
        else:
            content = _redact_secrets(content)
        return summary, ToolDetail(
            tool="NotebookEdit", summary=summary, input=full_input, path=path,
            content=content)

    elif name.startswith("mcp__"):
        parts = name.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else name
        detail_text = ""
        for key in ("query", "libraryId", "libraryName", "url", "prompt"):
            if inp.get(key):
                detail_text = f" ({_index_snippet(_redact_secrets(str(inp[key])), 80)})"
                break
        summary = f"MCP({server}): {tool}{detail_text}"
        query_val = _redact_secrets(
            str(inp.get("query", inp.get("libraryId", "")))
        )
        return summary, ToolDetail(
            tool=f"MCP({server}): {tool}", summary=summary, input=full_input,
            query=query_val)

    else:
        # Unknown / future tool. Claude Code ships new tools daily; rather than
        # collapse to a bare name, probe a broad key list, then fall back to the
        # first short scalar values in the input so the LLM-facing one-liner
        # still conveys the tool's payload. ALWAYS redact — arbitrary tools can
        # carry arbitrary secrets.
        _PROBE_KEYS = (
            "query", "question", "prompt", "file_path", "command", "url",
            "subject", "description", "status", "taskId", "script", "plan",
            "skill", "notebook_path", "pattern", "content", "name",
        )
        detail_text = ""
        for key in _PROBE_KEYS:
            if inp.get(key):
                detail_text = f": {_index_snippet(_redact_input_value_for_key(key, inp[key]), 80)}"
                break
        if not detail_text and isinstance(inp, dict):
            # Generic scalar render: join the first 2 short scalar values.
            scalars = []
            for key, v in inp.items():
                if isinstance(v, (str, int, float, bool)):
                    s = _redact_input_value_for_key(key, v).strip()
                    if s:
                        scalars.append(_index_snippet(s, 60))
                if len(scalars) >= 2:
                    break
            if scalars:
                detail_text = ": " + " ".join(scalars)
        summary = f"{name}{detail_text}"
        return summary, ToolDetail(
            tool=name, summary=summary,
            input=full_input,
            content=full_input,
        )


def _extract_tool_result_text(content) -> str | None:
    """Extract text from tool_result blocks. Keeps ALL results in full — no
    size cap, so the whole transcript is summarized (secrets are still
    redacted). claude -p's 10 MiB stdin limit is the only ceiling."""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        raw = "\n".join(parts)
    else:
        return None

    if not raw or not raw.strip():
        return None

    return _redact_secrets(raw).strip()


def _extract_user_tool_results(content) -> list[str]:
    """Extract tool_result text from a user message's content blocks."""
    if not isinstance(content, list):
        return []
    results = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            result_content = block.get("content", "")
            text = _extract_tool_result_text(result_content)
            if text:
                tool_id = block.get("tool_use_id", "")
                results.append(f"[result {tool_id}]: {text}")
    return results


def extract_session(jsonl_path: str) -> SessionDigest:
    """Parse a session JSONL and return structured content."""
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {jsonl_path}")

    project_slug = path.parent.name

    digest = SessionDigest(
        session_id=path.stem,
        project_path="",
        project_slug=project_slug,
        start_time="",
        end_time="",
        git_branch="",
    )

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")

            if entry_type == "file-history-snapshot":
                continue
            if entry.get("isMeta"):
                continue

            timestamp = entry.get("timestamp", "")
            if timestamp and not digest.start_time:
                digest.start_time = timestamp
            if timestamp:
                digest.end_time = timestamp

            cwd = entry.get("cwd", "")
            if cwd and not digest.project_path:
                digest.project_path = cwd

            branch = entry.get("gitBranch", "")
            if branch and not digest.git_branch:
                digest.git_branch = branch

            session_id = entry.get("sessionId", "")
            if session_id:
                digest.session_id = session_id

            message = entry.get("message", {})
            if not message:
                continue

            content = message.get("content", "")

            if entry_type == "user":
                text = _extract_text_from_content(content)
                tool_results = _extract_user_tool_results(content)

                if text is not None and _is_real_user_prompt(text):
                    clean_text = _redact_secrets(_SYSTEM_TAG_PATTERN.sub("", text).strip())
                    if clean_text:
                        digest.user_prompts.append(UserPrompt(
                            text=clean_text,
                            timestamp=timestamp,
                            uuid=entry.get("uuid", ""),
                        ))
                        digest.timeline.append(TimelineEntry(
                            role="user",
                            timestamp=timestamp,
                            text=clean_text,
                        ))

                if tool_results:
                    digest.timeline.append(TimelineEntry(
                        role="tool_result",
                        timestamp=timestamp,
                        text="",
                        tool_results=tool_results,
                    ))

            elif entry_type == "assistant":
                text_parts = []
                turn_tool_actions = []
                turn_tool_details = []

                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_parts.append(block.get("text", ""))
                        elif btype == "tool_use":
                            summary, detail = _extract_tool(block)
                            if summary:
                                digest.tool_actions.append(summary)
                                turn_tool_actions.append(summary)
                            if detail:
                                turn_tool_details.append(detail)
                        elif btype == "thinking":
                            # Claude Code stores reasoning signature-only (the
                            # plaintext is stripped on disk); capturing it would
                            # inject an opaque base64 blob. Record nothing.
                            pass
                        elif btype:
                            # Unknown / future block shape (e.g. 'image', or any
                            # new transcript block Claude Code adds). Capture a
                            # content-free marker so the activity reaches the
                            # summarizer without bloating the prompt with raw
                            # (possibly base64 / secret-bearing) payloads.
                            marker = f"[{btype} block]"
                            digest.tool_actions.append(marker)
                            turn_tool_actions.append(marker)
                    full_text = _redact_secrets("\n".join(text_parts).strip())
                    if full_text:
                        digest.assistant_responses.append(full_text)
                elif isinstance(content, str) and content.strip():
                    full_text = _redact_secrets(content.strip())
                    digest.assistant_responses.append(full_text)
                else:
                    full_text = ""

                digest.timeline.append(TimelineEntry(
                    role="assistant",
                    timestamp=timestamp,
                    text=full_text,
                    tool_actions=turn_tool_actions,
                    tool_details=turn_tool_details,
                ))

            elif entry_type:
                # Unknown / future top-level message type. Claude Code adds new
                # event kinds over time (agent-team / subagent / goal events,
                # etc.); capture any text they carry under their own role label
                # so the activity reaches the summarizer instead of vanishing.
                other_text = _extract_text_from_content(content)
                if other_text and other_text.strip():
                    digest.timeline.append(TimelineEntry(
                        role=entry_type,
                        timestamp=timestamp,
                        text=_redact_secrets(
                            _SYSTEM_TAG_PATTERN.sub("", other_text).strip()),
                    ))

    digest.total_turns = len(digest.timeline)

    # Fallback: derive timestamps from file mtime when JSONL has none
    if not digest.start_time:
        from datetime import datetime, timezone
        mtime = path.stat().st_mtime
        iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest.start_time = iso
        if not digest.end_time:
            digest.end_time = iso

    return digest


def digest_to_text(digest: SessionDigest, max_chars: int | None = None) -> str:
    """Format a digest as an interleaved timeline for the LLM prompt.

    One-liner tool indexes plus full redacted tool inputs; the whole timeline is
    included. max_chars is retained only for call-site compatibility; Chronicle
    ignores it and imposes no front/tail truncation. claude -p's 10 MiB stdin
    limit is the only ceiling.
    """
    parts = []

    parts.append("=== SESSION METADATA ===")
    parts.append(f"session_id: {digest.session_id}")
    parts.append(f"project: {digest.project_path}")
    parts.append(f"branch: {digest.git_branch}")
    parts.append(f"time: {digest.start_time} -> {digest.end_time}")
    parts.append(f"turns: {digest.total_turns}")
    parts.append("")
    parts.append("=== TIMELINE ===")

    timeline_parts = []
    for turn in digest.timeline:
        ts = turn.timestamp[:19] if turn.timestamp else ""

        if turn.role == "user":
            timeline_parts.append(f"\n[{ts}] USER")
            timeline_parts.append(turn.text)

        elif turn.role == "assistant":
            timeline_parts.append(f"\n[{ts}] ASSISTANT")
            if turn.text:
                timeline_parts.append(turn.text)
            if turn.tool_actions or turn.tool_details:
                timeline_parts.append("TOOLS:")
                detail_index = 0
                for action in turn.tool_actions:
                    if (
                        detail_index < len(turn.tool_details)
                        and action == turn.tool_details[detail_index].summary
                    ):
                        detail = turn.tool_details[detail_index]
                        timeline_parts.append(f"  - {detail.summary}")
                        if detail.input:
                            timeline_parts.append("    FULL INPUT:")
                            for line in detail.input.split("\n"):
                                timeline_parts.append(f"      {line}")
                        detail_index += 1
                    else:
                        timeline_parts.append(f"  - {action}")
                for detail in turn.tool_details[detail_index:]:
                    timeline_parts.append(f"  - {detail.summary}")
                    if detail.input:
                        timeline_parts.append("    FULL INPUT:")
                        for line in detail.input.split("\n"):
                            timeline_parts.append(f"      {line}")

        elif turn.role == "tool_result":
            if turn.tool_results:
                timeline_parts.append(f"\n[{ts}] TOOL OUTPUT")
                for result in turn.tool_results:
                    timeline_parts.append(result)

        elif turn.text:
            # Unknown / future role (see extract_session): label it generically
            # so new transcript shapes are summarized rather than dropped.
            timeline_parts.append(f"\n[{ts}] {turn.role.upper()}")
            timeline_parts.append(turn.text)

    timeline_text = "\n".join(timeline_parts)

    # max_chars is deliberately ignored: Chronicle must not impose its own
    # transcript truncation before claude -p's stdin limit.
    parts.append(timeline_text)
    return "\n".join(parts)


def _markdown_fence(text: str, min_ticks: int = 4) -> str:
    """Return a backtick fence longer than any backtick run in text."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text or "")), default=0)
    return "`" * max(min_ticks, longest + 1)


def _detail_size(text: str) -> str:
    line_count = text.count("\n") + 1 if text else 0
    line_label = "line" if line_count == 1 else "lines"
    return f"{len(text):,} chars, {line_count:,} {line_label}"


def _append_verbatim_details(lines: list[str], summary: str, content: str) -> None:
    """Append a collapsible, fenced verbatim block to a markdown line buffer."""
    if not content:
        return
    lines.append(f"<details><summary>{html.escape(summary, quote=False)}</summary>")
    lines.append("")
    fence = _markdown_fence(content)
    lines.append(fence)
    lines.append(content)
    lines.append(fence)
    lines.append("")
    lines.append("</details>")


def _append_tool_input_details(lines: list[str], td: ToolDetail) -> None:
    if td.input:
        _append_verbatim_details(
            lines,
            f"Full {td.tool} input ({_detail_size(td.input)})",
            td.input,
        )


def timeline_to_log(digest: SessionDigest) -> str:
    """Generate a full chronological log from the timeline.

    No size truncation. Content is extracted/redacted, thinking blocks are
    skipped, and full tool inputs/outputs are kept in collapsible fenced blocks.
    This is the archival record in the session markdown.
    """
    lines = []
    turn_num = 0

    for turn in digest.timeline:
        ts = turn.timestamp[11:19] if turn.timestamp and len(turn.timestamp) > 19 else ""

        if turn.role == "user":
            turn_num += 1
            lines.append(f"\n[{ts}] USER #{turn_num}:")
            for line in turn.text.split("\n"):
                lines.append(f"  {line}")

        elif turn.role == "assistant":
            lines.append(f"\n[{ts}] ASSISTANT:")
            if turn.text:
                for line in turn.text.split("\n"):
                    lines.append(f"  {line}")

            # Full tool details
            for td in turn.tool_details:
                lines.append("")
                if td.tool == "Edit" and td.path:
                    lines.append(f"  EDIT: {td.path}")
                    if td.old_content:
                        _append_verbatim_details(
                            lines,
                            f"Edit old_string ({_detail_size(td.old_content)})",
                            td.old_content,
                        )
                    if td.content:
                        _append_verbatim_details(
                            lines,
                            f"Edit new_string ({_detail_size(td.content)})",
                            td.content,
                        )

                elif td.tool == "Write" and td.path:
                    lines.append(f"  WRITE: {td.path}")
                    if td.content:
                        _append_verbatim_details(
                            lines,
                            f"Write content ({_detail_size(td.content)})",
                            td.content,
                        )

                elif td.tool == "Bash":
                    lines.append(f"  BASH: {_index_snippet(td.command, 160)}")

                elif td.tool == "Read":
                    lines.append(f"  READ: {td.path}")

                elif td.tool == "Agent":
                    lines.append(f"  AGENT: {_index_snippet(td.description, 160)}")
                    if td.content:
                        _append_verbatim_details(
                            lines,
                            f"Agent prompt ({_detail_size(td.content)})",
                            td.content,
                        )

                elif td.tool in ("Glob", "Grep"):
                    lines.append(f"  {td.tool.upper()}: {_index_snippet(td.query, 160)}")

                elif td.tool in ("WebSearch", "WebFetch", "Skill"):
                    lines.append(f"  {td.tool.upper()}: {_index_snippet(td.query, 160)}")

                elif td.tool.startswith("MCP("):
                    suffix = f": {_index_snippet(td.query, 160)}" if td.query else ""
                    lines.append(f"  {td.tool}{suffix}")

                else:
                    lines.append(f"  {td.summary}")

                _append_tool_input_details(lines, td)

        elif turn.role == "tool_result":
            for result in turn.tool_results:
                lines.append(f"\n[{ts}] TOOL OUTPUT:")
                first_line = result.splitlines()[0] if result.splitlines() else ""
                if first_line:
                    lines.append(f"  {_index_snippet(first_line, 160)}")
                _append_verbatim_details(
                    lines,
                    f"Full tool output ({_detail_size(result)})",
                    result,
                )

        elif turn.text:
            # Unknown / future role: label it generically so the archival log
            # records new transcript shapes instead of dropping them.
            lines.append(f"\n[{ts}] {turn.role.upper()}:")
            for line in turn.text.split("\n"):
                lines.append(f"  {line}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python extractor.py <session.jsonl>")
        sys.exit(1)

    digest = extract_session(sys.argv[1])
    print(f"Session: {digest.session_id}")
    print(f"Project: {digest.project_path} ({digest.project_slug})")
    print(f"Branch: {digest.git_branch}")
    print(f"Time: {digest.start_time} -> {digest.end_time}")
    print(f"Turns: {digest.total_turns}")
    print(f"User prompts: {len(digest.user_prompts)}")
    print(f"Assistant responses: {len(digest.assistant_responses)}")
    print(f"Timeline entries: {len(digest.timeline)}")
    print(f"Tool actions: {len(digest.tool_actions)}")
    print()
    print(digest_to_text(digest)[:3000])
