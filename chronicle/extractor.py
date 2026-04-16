"""Parse Claude Code session JSONL files and extract meaningful content.

The JSONL files at ~/.claude/projects/<slug>/<session-id>.jsonl contain every
message exchanged in a session. This module extracts structured content
preserving chronological order for high-fidelity summarization.

Two output formats:
- digest_to_text(): filtered/truncated for LLM context window (80K chars default)
- timeline_to_log(): full raw content for the session markdown (no truncation)
"""

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
    """Rich detail about a tool call — full inputs for the archival record."""
    tool: str
    summary: str  # one-liner for the LLM prompt
    path: str = ""
    command: str = ""
    content: str = ""  # Write content or Edit new_string
    old_content: str = ""  # Edit old_string
    query: str = ""  # WebSearch, Grep, Glob
    description: str = ""  # Agent


@dataclass
class TimelineEntry:
    """A single turn in the chronological conversation timeline."""
    role: str  # "user", "assistant", "tool_result"
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
    r"(?:export\s+)?(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS|AUTH|PRIVATE_KEY|ACCESS_KEY)"
    r"[_A-Z]*[\s]*[=:]\s*\S+|"
    r"Bearer\s+\S+|"
    r"(?:sk-|pk-|ghp_|gho_|github_pat_|xoxb-|xoxp-|sk_live_|sk_test_|rk_live_|rk_test_|AKIA)\S+|"
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


def _redact_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]."""
    if not text:
        return text
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


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

_MAX_TOOL_RESULT_CHARS = 10000


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

    if name == "Bash":
        cmd = _redact_secrets(inp.get("command", ""))
        return f"Bash: {cmd[:120]}", ToolDetail(
            tool="Bash", summary=f"Bash: {cmd[:120]}", command=cmd)

    elif name == "Edit":
        path = inp.get("file_path", "")
        old = _redact_secrets(inp.get("old_string", ""))
        new = _redact_secrets(inp.get("new_string", ""))
        return f"Edit: {path}", ToolDetail(
            tool="Edit", summary=f"Edit: {path}", path=path,
            old_content=old, content=new)

    elif name == "Write":
        path = inp.get("file_path", "")
        content = inp.get("content", "")
        # Fully redact content for sensitive file types
        if _SENSITIVE_PATHS.search(path):
            content = f"[REDACTED — sensitive file: {Path(path).name}]"
        else:
            content = _redact_secrets(content)
        return f"Write: {path}", ToolDetail(
            tool="Write", summary=f"Write: {path}", path=path, content=content)

    elif name == "Read":
        path = inp.get("file_path", "")
        return f"Read: {path}", ToolDetail(
            tool="Read", summary=f"Read: {path}", path=path)

    elif name in ("Glob", "Grep"):
        pattern = _redact_secrets(inp.get("pattern", ""))
        return f"{name}: {pattern}", ToolDetail(
            tool=name, summary=f"{name}: {pattern}", query=pattern)

    elif name == "Agent":
        desc = _redact_secrets(inp.get("description", ""))
        prompt = _redact_secrets(inp.get("prompt", ""))
        return f"Agent: {desc}", ToolDetail(
            tool="Agent", summary=f"Agent: {desc}",
            description=desc, content=prompt[:500])

    elif name == "Skill":
        skill = inp.get("skill", "")
        return f"Skill: {skill}", ToolDetail(
            tool="Skill", summary=f"Skill: {skill}", query=skill)

    elif name == "WebSearch":
        query = _redact_secrets(inp.get("query", ""))
        return f"WebSearch: {query}", ToolDetail(
            tool="WebSearch", summary=f"WebSearch: {query}", query=query)

    elif name == "WebFetch":
        url = _redact_secrets(inp.get("url", ""))
        return f"WebFetch: {url}", ToolDetail(
            tool="WebFetch", summary=f"WebFetch: {url}", query=url)

    elif name == "MultiEdit":
        path = inp.get("file_path", "")
        edits = inp.get("edits", [])
        count = len(edits)
        return f"MultiEdit: {path} ({count} regions)", ToolDetail(
            tool="MultiEdit", summary=f"MultiEdit: {path} ({count} regions)",
            path=path, content=f"{count} edit regions")

    elif name.startswith("mcp__"):
        parts = name.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else name
        detail_text = ""
        for key in ("query", "libraryId", "libraryName", "url", "prompt"):
            if inp.get(key):
                detail_text = f" ({_redact_secrets(str(inp[key]))[:80]})"
                break
        summary = f"MCP({server}): {tool}{detail_text}"
        query_val = _redact_secrets(
            str(inp.get("query", inp.get("libraryId", "")))
        )
        return summary, ToolDetail(
            tool=f"MCP({server}): {tool}", summary=summary, query=query_val)

    else:
        detail_text = ""
        for key in ("query", "question", "prompt", "file_path", "command", "url"):
            if inp.get(key):
                detail_text = f": {_redact_secrets(str(inp[key]))[:80]}"
                break
        summary = f"{name}{detail_text}"
        # The full input dict can carry arbitrary secrets from arbitrary MCP
        # / custom tools — always redact the string dump.
        return summary, ToolDetail(
            tool=name, summary=summary,
            content=_redact_secrets(str(inp))[:500],
        )


def _extract_tool_result_text(content) -> str | None:
    """Extract text from tool_result blocks. Keeps ALL results (no signal filter)."""
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

    # Redact secrets before truncation
    raw = _redact_secrets(raw)

    # Cap at 10KB with front+tail for very large results
    if len(raw) > _MAX_TOOL_RESULT_CHARS:
        half = _MAX_TOOL_RESULT_CHARS // 2
        raw = raw[:half] + "\n[... truncated ...]\n" + raw[-half:]

    return raw.strip()


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
                tool_id = block.get("tool_use_id", "")[:8]
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
                    clean_text = _SYSTEM_TAG_PATTERN.sub("", text).strip()
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
                    full_text = "\n".join(text_parts).strip()
                    if full_text:
                        digest.assistant_responses.append(full_text)
                elif isinstance(content, str) and content.strip():
                    full_text = content.strip()
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


def digest_to_text(digest: SessionDigest, max_chars: int = 80000) -> str:
    """Format a digest as an interleaved timeline for the LLM prompt.

    Uses one-liner tool summaries and front+tail truncation for context window.
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
            if turn.tool_actions:
                timeline_parts.append("TOOLS:")
                prev = None
                count = 0
                for action in turn.tool_actions:
                    if action == prev:
                        count += 1
                    else:
                        if prev is not None:
                            suffix = f" (x{count})" if count > 1 else ""
                            timeline_parts.append(f"  - {prev}{suffix}")
                        prev = action
                        count = 1
                if prev is not None:
                    suffix = f" (x{count})" if count > 1 else ""
                    timeline_parts.append(f"  - {prev}{suffix}")

        elif turn.role == "tool_result":
            if turn.tool_results:
                timeline_parts.append(f"\n[{ts}] TOOL OUTPUT")
                for result in turn.tool_results:
                    timeline_parts.append(result)

    timeline_text = "\n".join(timeline_parts)

    if len(timeline_text) > max_chars:
        front_budget = int(max_chars * 0.75)
        tail_budget = max_chars - front_budget
        front = timeline_text[:front_budget]
        tail = timeline_text[-tail_budget:]
        omitted = len(timeline_text) - max_chars
        timeline_text = (
            front
            + f"\n\n[... {omitted:,} chars from middle of session omitted ...]\n\n"
            + tail
        )

    parts.append(timeline_text)
    return "\n".join(parts)


def timeline_to_log(digest: SessionDigest) -> str:
    """Generate a full chronological log from the timeline.

    No truncation. Full raw content. Human-readable formatting.
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
                        lines.append("    - old:")
                        for dl in td.old_content.split("\n"):
                            lines.append(f"      {dl}")
                    if td.content:
                        lines.append("    + new:")
                        for dl in td.content.split("\n"):
                            lines.append(f"      {dl}")

                elif td.tool == "Write" and td.path:
                    lines.append(f"  WRITE: {td.path}")
                    if td.content:
                        lines.append("    CONTENT:")
                        for dl in td.content.split("\n"):
                            lines.append(f"      {dl}")

                elif td.tool == "Bash":
                    lines.append(f"  BASH: {td.command}")

                elif td.tool == "Read":
                    lines.append(f"  READ: {td.path}")

                elif td.tool == "Agent":
                    lines.append(f"  AGENT: {td.description}")
                    if td.content:
                        lines.append(f"    prompt: {td.content}")

                elif td.tool in ("Glob", "Grep"):
                    lines.append(f"  {td.tool.upper()}: {td.query}")

                elif td.tool in ("WebSearch", "WebFetch", "Skill"):
                    lines.append(f"  {td.tool.upper()}: {td.query}")

                elif td.tool.startswith("MCP("):
                    lines.append(f"  {td.tool}: {td.query}")

                else:
                    lines.append(f"  {td.summary}")

        elif turn.role == "tool_result":
            for result in turn.tool_results:
                lines.append(f"\n[{ts}] TOOL OUTPUT:")
                for line in result.split("\n"):
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
