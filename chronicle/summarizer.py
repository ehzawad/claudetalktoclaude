"""Session summarization via the Claude CLI.

Builds a SESSION prompt from the extracted transcript and invokes
`claude -p --json-schema` via chronicle.claude_cli.spawn_claude (which
handles binary resolution, env sanitization, subprocess registry, and
error classification).

Uses --effort max for thorough reasoning and --fallback-model for
resilience when the primary model is overloaded.

Provides `async_summarize_session()` for parallel processing.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field

from .claude_cli import ErrorKind, spawn_claude
from .config import load_config
from .extractor import SessionDigest, digest_to_text, timeline_to_log

# JSON Schema for structured output validation via --json-schema.
# Claude must return data matching this schema; the CLI validates it.
CHRONICLE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "is_empty": {
            "type": "boolean",
            "description": "True if session has no meaningful technical work",
        },
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "narrative": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "status": {"type": "string", "enum": ["made", "rejected", "tentative"]},
                    "why": {"type": "string"},
                    "context": {"type": "string"},
                    "alternatives_considered": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "numbers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["what", "why"],
            },
        },
        "problems_solved": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "solution": {"type": "string"},
                    "verification": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["problem", "solution"],
            },
        },
        "human_reasoning": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "moment": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["moment", "reasoning"],
            },
        },
        "follow_ups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "context": {"type": "string"},
                    "outcome": {"type": "string"},
                },
                "required": ["question"],
            },
        },
        "technical_details": {
            "type": "object",
            "properties": {
                "stack": {"type": "array", "items": {"type": "string"}},
                "numbers": {"type": "array", "items": {"type": "string"}},
                "commands": {"type": "array", "items": {"type": "string"}},
                "errors": {"type": "array", "items": {"type": "string"}},
                "config": {"type": "array", "items": {"type": "string"}},
            },
        },
        "architecture": {
            "type": "object",
            "properties": {
                "project_structure": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "data_flow": {"type": "string"},
            },
        },
        "planning": {
            "type": "object",
            "properties": {
                "initial_plan": {"type": "string"},
                "plan_changes": {"type": "array", "items": {"type": "string"}},
                "work_breakdown": {"type": "array", "items": {"type": "string"}},
                "deferred": {"type": "array", "items": {"type": "string"}},
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "cross_references": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["is_empty", "title"],
}

SUMMARIZATION_PROMPT = """\
You are writing a high-fidelity engineering chronicle from a Claude Code session transcript.

Your job is not to compress the session into a clean success story. Your job is to \
preserve the technical reasoning with enough fidelity that another engineer can \
reconstruct what happened, why it happened, what failed, and what evidence changed \
the developer's mind.

NON-NEGOTIABLE RULES:
- If the session contains no meaningful technical work, set is_empty to true and \
provide only a brief title. All other fields are optional when is_empty is true.
- Preserve chronology. Keep cause -> investigation -> decision -> verification in order.
- Preserve exact concrete facts: filenames, commands, flags, config keys, env vars, \
versions, counts, timings, sizes, model names, error text, exit codes, benchmark results.
- Preserve the human's reasoning and constraints, not just Claude's actions.
- Preserve rejected options and why they were rejected.
- Preserve debugging as a sequence: symptom -> evidence -> hypotheses -> attempted \
fixes -> root cause -> final fix -> verification.
- Prefer exact values over vague language. Write "p95 dropped from 480ms to 170ms", \
not "performance improved".
- Do not turn tentative conclusions into certainty. Mark them as tentative.
- Do not omit failed attempts just because the final result worked.
- Exclude routine navigation only if it added no evidence.
- CAPTURE PROJECT STRUCTURE: When the session explores or establishes a codebase layout, \
describe the directory structure, module boundaries, and how components connect.
- CAPTURE ARCHITECTURE: When architectural patterns are discussed or established (API design, \
data flow, service boundaries, state management), document them with the rationale.
- CAPTURE PLANNING: When work is broken into phases, chunks, or steps, document the plan, \
what order was chosen and why, what was deferred, and how the plan evolved mid-session.
- CAPTURE PLAN CHANGES: If a plan was created then revised or rejected, document both the \
original plan and what changed, with the reason for the pivot.
- CAPTURE FOLLOW-UPS: When the developer asks clarifying questions ("wait, how does X work?", \
"what if we tried Y?", "I don't understand why..."), capture both the question and what was \
learned. These moments reveal what wasn't obvious and what the developer needed to understand.
- CAPTURE BACK-AND-FORTH: When there's a genuine dialogue — pushback, course corrections, \
"actually no, do it this way" — preserve that exchange. It shows how the approach was shaped.
- WRITE LIKE A DEVELOPER TALKING: The narrative should read like an engineer explaining their \
thought process to a colleague over coffee, not like a formal report. Use "we tried", "turns out", \
"the problem was", "so instead we". Include the moments of confusion, realization, and discovery.

Return structured output matching the provided JSON Schema. When in doubt, include more \
technical detail rather than less.

SESSION TRANSCRIPT:
{transcript}
"""


@dataclass
class ChronicleEntry:
    session_id: str
    project_path: str
    project_slug: str
    start_time: str
    end_time: str
    git_branch: str
    user_prompts: list  # UserPrompt objects
    title: str = ""
    summary: str = ""
    narrative: str = ""
    decisions: list = field(default_factory=list)
    problems_solved: list = field(default_factory=list)
    human_reasoning: list = field(default_factory=list)
    follow_ups: list = field(default_factory=list)
    technical_details: dict = field(default_factory=dict)
    architecture: dict = field(default_factory=dict)
    planning: dict = field(default_factory=dict)
    open_questions: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    cross_references: list = field(default_factory=list)
    is_empty: bool = False
    is_error: bool = False  # summarization failed
    error_kind: str = ""  # "infra" | "transient" | "parse" | "" (no error)
    error_message: str = ""
    total_turns: int = 0
    tool_actions: list = field(default_factory=list)
    turn_log: str = ""  # one-liner-per-turn chronological log
    total_cost_usd: float = 0.0


def _make_entry(digest: SessionDigest) -> ChronicleEntry:
    """Create a blank ChronicleEntry from a SessionDigest."""
    return ChronicleEntry(
        session_id=digest.session_id,
        project_path=digest.project_path,
        project_slug=digest.project_slug,
        start_time=digest.start_time,
        end_time=digest.end_time,
        git_branch=digest.git_branch,
        user_prompts=digest.user_prompts,
        total_turns=digest.total_turns,
        tool_actions=digest.tool_actions,
        turn_log=timeline_to_log(digest),
    )


def _populate_entry_from_structured(data: dict, entry: ChronicleEntry) -> ChronicleEntry:
    """Fill a ChronicleEntry from a validated structured_output dict."""
    if data.get("is_empty"):
        entry.is_empty = True
        entry.title = data.get("title", f"Session {entry.session_id[:8]}")
        return entry

    entry.title = data.get("title", "Untitled session")
    entry.summary = data.get("summary", "")
    entry.narrative = data.get("narrative", "")
    entry.decisions = data.get("decisions", [])
    entry.problems_solved = data.get("problems_solved", [])
    entry.human_reasoning = data.get("human_reasoning", [])
    entry.follow_ups = data.get("follow_ups", [])
    entry.technical_details = data.get("technical_details", {})
    entry.architecture = data.get("architecture", {})
    entry.planning = data.get("planning", {})
    entry.open_questions = data.get("open_questions", [])
    entry.files_changed = data.get("files_changed", [])
    entry.cross_references = data.get("cross_references", [])
    return entry


def _extract_structured(outer: dict) -> dict | None:
    """Return the structured_output dict from the outer JSON wrapper, or None.

    Falls back to parsing outer["result"] as JSON for CLI versions without
    --json-schema support.
    """
    data = outer.get("structured_output")
    if isinstance(data, dict):
        return data
    raw_text = (outer.get("result") or "").strip()
    if not raw_text:
        return None
    try:
        loaded = json.loads(raw_text)
        return loaded if isinstance(loaded, dict) else None
    except (ValueError, TypeError):
        return None


async def async_summarize_session(digest: SessionDigest) -> ChronicleEntry:
    """One-shot summarization via claude -p with --json-schema validation.

    Uses --no-session-persistence so observer calls never appear in the user's
    session list. Uses --effort max for thorough reasoning and --fallback-model
    for resilience when the primary model is overloaded.

    Classifies failures via claude_cli.ErrorKind so the caller (storage.write_chronicle)
    can distinguish infrastructure errors (don't charge retries) from transient /
    parse errors (do charge retries).
    """
    config = load_config()
    model = config.get("model", "opus")
    fallback = config.get("fallback_model", "sonnet")

    entry = _make_entry(digest)

    # Sessions with no actual content — return empty entry directly
    if not digest.timeline and not digest.user_prompts:
        entry.is_empty = True
        entry.title = f"Session {digest.session_id[:8]}"
        return entry

    transcript = digest_to_text(digest)
    base_prompt = SUMMARIZATION_PROMPT.format(transcript=transcript)

    from .config import load_recent_titles
    titles = load_recent_titles(digest.project_slug, max_entries=5)
    if titles:
        recent = "Recent sessions in this project:\n" + "\n".join(f"- {t}" for t in titles)
        prompt = f"{recent}\n\nIf you see connections to these previous sessions, " \
                 f"include a \"cross_references\" field.\n\n{base_prompt}"
    else:
        prompt = base_prompt

    result = await spawn_claude(
        prompt=prompt,
        model=model,
        fallback_model=fallback,
        effort="max",
        json_schema=CHRONICLE_JSON_SCHEMA,
        timeout=300,
    )

    entry.total_cost_usd = result.total_cost_usd

    if result.error_kind is not None:
        entry.is_error = True
        entry.error_kind = result.error_kind.value
        entry.error_message = result.error_message
        print(f"[chronicle] summarization {result.error_kind.value}: "
              f"{result.error_message[:200]}", file=sys.stderr)
        return entry

    outer = result.stdout_json or {}
    data = _extract_structured(outer)
    if data is None:
        # No structured output AND empty result text — treat as genuinely empty
        if not (outer.get("result") or "").strip():
            entry.is_empty = True
            return entry
        entry.is_error = True
        entry.error_kind = ErrorKind.PARSE.value
        entry.error_message = "structured_output missing and result not JSON"
        return entry

    return _populate_entry_from_structured(data, entry)


def entry_to_session_markdown(entry: ChronicleEntry) -> str:
    """Format a ChronicleEntry as a detailed per-session markdown record."""
    lines = []
    short_id = entry.session_id[:8]
    ts = entry.start_time[:19].replace("T", " ") if entry.start_time else "unknown"

    lines.append(f"# {entry.title or f'Session {short_id}'}")
    lines.append("")
    lines.append(f"**Session**: {short_id} | **Date**: {ts} | "
                 f"**Branch**: {entry.git_branch} | **Turns**: {entry.total_turns}")
    lines.append(f"**Project**: {entry.project_path}")
    if entry.total_cost_usd > 0:
        lines.append(f"**Cost**: ${entry.total_cost_usd:.2f}")
    lines.append("")

    # Summary
    if entry.summary:
        lines.append("## Summary")
        lines.append("")
        lines.append(entry.summary)
        lines.append("")

    # Narrative (the main content)
    if entry.narrative:
        lines.append("## What happened")
        lines.append("")
        lines.append(entry.narrative)
        lines.append("")

    # Decisions with full context
    if entry.decisions:
        lines.append("## Key decisions")
        lines.append("")
        for d in entry.decisions:
            what = d.get("what", d) if isinstance(d, dict) else str(d)
            status = d.get("status", "") if isinstance(d, dict) else ""
            why = d.get("why", "") if isinstance(d, dict) else ""
            context = d.get("context", "") if isinstance(d, dict) else ""
            alternatives = d.get("alternatives_considered", []) if isinstance(d, dict) else []
            numbers = d.get("numbers", []) if isinstance(d, dict) else []
            status_tag = f" _{status}_" if status and status != "made" else ""
            lines.append(f"### {what}{status_tag}")
            if context:
                lines.append(f"**Context**: {context}")
            if why:
                lines.append(f"**Rationale**: {why}")
            if alternatives:
                lines.append("**Alternatives considered**:")
                for alt in alternatives:
                    lines.append(f"- {alt}")
            if numbers:
                for n in numbers:
                    lines.append(f"- {n}")
            lines.append("")

    # Problems solved
    if entry.problems_solved:
        lines.append("## Problems solved")
        lines.append("")
        for p in entry.problems_solved:
            if isinstance(p, dict):
                problem = p.get("problem", "")
                diagnosis = p.get("diagnosis", "")
                solution = p.get("solution", "")
                verification = p.get("verification", "")
                evidence = p.get("evidence", [])
                lines.append(f"**Problem**: {problem}")
                if diagnosis:
                    lines.append(f"**Diagnosis**: {diagnosis}")
                if solution:
                    lines.append(f"**Solution**: {solution}")
                if verification:
                    lines.append(f"**Verification**: {verification}")
                if evidence:
                    for e in evidence:
                        lines.append(f"- `{e}`")
                lines.append("")
            else:
                lines.append(f"- {p}")

    # Human reasoning
    if entry.human_reasoning:
        lines.append("## Developer reasoning")
        lines.append("")
        for hr in entry.human_reasoning:
            if isinstance(hr, dict):
                moment = hr.get("moment", "")
                reasoning = hr.get("reasoning", "")
                lines.append(f"**{moment}**")
                if reasoning:
                    lines.append(reasoning)
                lines.append("")
            else:
                lines.append(f"- {hr}")

    # Follow-ups and clarifications
    if entry.follow_ups:
        lines.append("## Follow-ups & clarifications")
        lines.append("")
        for fu in entry.follow_ups:
            if isinstance(fu, dict):
                question = fu.get("question", "")
                context = fu.get("context", "")
                outcome = fu.get("outcome", "")
                lines.append(f"**Q: {question}**")
                if context:
                    lines.append(f"*Context*: {context}")
                if outcome:
                    lines.append(f"*Outcome*: {outcome}")
                lines.append("")
            else:
                lines.append(f"- {fu}")

    # Technical details
    td = entry.technical_details
    if td and any(td.get(k) for k in ("stack", "numbers", "commands", "errors", "config")):
        lines.append("## Technical details")
        lines.append("")
        if td.get("stack"):
            lines.append("**Stack**: " + ", ".join(td["stack"]))
        if td.get("numbers"):
            lines.append("")
            for n in td["numbers"]:
                lines.append(f"- {n}")
        if td.get("errors"):
            lines.append("")
            lines.append("**Errors encountered**:")
            for e in td["errors"]:
                lines.append(f"- `{e}`")
        if td.get("commands"):
            lines.append("")
            lines.append("**Key commands**:")
            for c in td["commands"]:
                lines.append(f"- `{c}`")
        if td.get("config"):
            lines.append("")
            lines.append("**Config/API details**:")
            for c in td["config"]:
                lines.append(f"- {c}")
        lines.append("")

    # Architecture
    arch = entry.architecture
    if arch and any(arch.get(k) for k in ("project_structure", "patterns", "data_flow")):
        lines.append("## Architecture")
        lines.append("")
        if arch.get("project_structure"):
            lines.append(arch["project_structure"])
            lines.append("")
        if arch.get("patterns"):
            lines.append("**Patterns**:")
            for p in arch["patterns"]:
                lines.append(f"- {p}")
            lines.append("")
        if arch.get("data_flow"):
            lines.append(f"**Data flow**: {arch['data_flow']}")
            lines.append("")

    # Planning
    plan = entry.planning
    if plan and any(plan.get(k) for k in ("initial_plan", "plan_changes", "work_breakdown", "deferred")):
        lines.append("## Planning")
        lines.append("")
        if plan.get("initial_plan"):
            lines.append(f"**Initial plan**: {plan['initial_plan']}")
            lines.append("")
        if plan.get("plan_changes"):
            lines.append("**Plan evolution**:")
            for change in plan["plan_changes"]:
                lines.append(f"- {change}")
            lines.append("")
        if plan.get("work_breakdown"):
            lines.append("**Work breakdown**:")
            for step in plan["work_breakdown"]:
                lines.append(f"- {step}")
            lines.append("")
        if plan.get("deferred"):
            lines.append("**Deferred**:")
            for d in plan["deferred"]:
                lines.append(f"- {d}")
            lines.append("")

    # Open questions
    if entry.open_questions:
        lines.append("## Open questions")
        lines.append("")
        for q in entry.open_questions:
            lines.append(f"- {q}")
        lines.append("")

    # Cross-references to past sessions
    if entry.cross_references:
        lines.append("## Cross-references")
        lines.append("")
        for ref in entry.cross_references:
            lines.append(f"- {ref}")
        lines.append("")

    # Files changed
    if entry.files_changed:
        lines.append("## Files changed")
        lines.append("")
        for f in entry.files_changed:
            lines.append(f"- `{f}`")
        lines.append("")

    # Turn-by-turn chronological log
    if entry.turn_log:
        lines.append("## Turn-by-turn log")
        lines.append("")
        lines.append("```")
        lines.append(entry.turn_log)
        lines.append("```")
        lines.append("")

    # User prompts (verbatim) at the end as reference
    if entry.user_prompts:
        lines.append("---")
        lines.append("")
        lines.append("<details><summary>User prompts (verbatim)</summary>")
        lines.append("")
        for i, prompt in enumerate(entry.user_prompts, 1):
            pts = prompt.timestamp[:19].replace("T", " ") if prompt.timestamp else ""
            lines.append(f"**Prompt {i}** ({pts}):")
            for pline in prompt.text.split("\n"):
                lines.append(f"> {pline}")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    from .extractor import extract_session

    if len(sys.argv) < 2:
        print("Usage: python -m chronicle.summarizer <session.jsonl>")
        sys.exit(1)

    digest = extract_session(sys.argv[1])
    print(f"Extracted {len(digest.user_prompts)} prompts, {len(digest.assistant_responses)} responses")
    print(f"Sending to Claude for summarization...")

    entry = asyncio.run(async_summarize_session(digest))
    if entry.is_empty:
        print("No meaningful decisions found.")
    else:
        print("\n" + entry_to_session_markdown(entry))
