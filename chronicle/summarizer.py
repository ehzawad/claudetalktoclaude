"""Call Claude (via `claude -p`) to summarize session content into decisions.

Uses `claude -p --model <model>` for summarization via your subscription.

Provides `async_summarize_session()` for parallel processing of multiple sessions.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field

from .config import load_config
from .extractor import SessionDigest, digest_to_text, timeline_to_log

SUMMARIZATION_PROMPT = """\
You are writing a high-fidelity engineering chronicle from a Claude Code session transcript.

Your job is not to compress the session into a clean success story. Your job is to \
preserve the technical reasoning with enough fidelity that another engineer can \
reconstruct what happened, why it happened, what failed, and what evidence changed \
the developer's mind.

NON-NEGOTIABLE RULES:
- If the session contains no meaningful technical work, respond exactly: NO_DECISIONS
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

Return valid JSON only. No markdown fences.

Use this schema:
{{
  "title": "Specific and technical title",
  "summary": "2-4 dense sentences: main goal, major decision, biggest obstacle, \
current landing point.",
  "decisions": [
    {{
      "what": "The specific decision or choice",
      "status": "made|rejected|tentative",
      "why": "Full rationale, including constraints and trade-offs",
      "context": "What prompted this decision",
      "alternatives_considered": ["Alternative A and why rejected"],
      "evidence": ["Turn or command references supporting this"],
      "numbers": ["Relevant exact numbers, versions, timings"]
    }}
  ],
  "problems_solved": [
    {{
      "problem": "The concrete failure or obstacle",
      "diagnosis": "How it was investigated and what evidence pointed to the cause",
      "solution": "What changed",
      "verification": "How the fix was confirmed",
      "evidence": ["Exact error text, command output, or turn references"]
    }}
  ],
  "human_reasoning": [
    {{
      "moment": "Where the human reframed the problem or made a judgment call",
      "reasoning": "What they were optimizing for or worried about",
      "evidence": ["Relevant prompt text"]
    }}
  ],
  "follow_ups": [
    {{
      "question": "What the developer asked or pushed back on",
      "context": "Why they asked — what was confusing or what they wanted to validate",
      "outcome": "What was learned or what changed as a result"
    }}
  ],
  "technical_details": {{
    "stack": ["Libraries, frameworks, tools, services, APIs"],
    "numbers": ["Benchmarks, timings, sizes, versions, counts"],
    "commands": ["Commands that materially informed a decision"],
    "errors": ["Important errors, warnings, or failing assertions"],
    "config": ["Config keys, flags, endpoints, env vars, schema details"]
  }},
  "architecture": {{
    "project_structure": "How the codebase is organized — key directories, module \
boundaries, entry points. Only if discussed or established in this session.",
    "patterns": ["Architectural patterns used or established (e.g., 'FastAPI + \
async workers + Redis queue', 'two-pass LLM pipeline')"],
    "data_flow": "How data moves through the system, if discussed"
  }},
  "planning": {{
    "initial_plan": "What was the original plan or approach at the start of the session",
    "plan_changes": ["Each revision: what changed and why"],
    "work_breakdown": ["How the work was chunked into steps or phases"],
    "deferred": ["What was explicitly deferred to later"]
  }},
  "open_questions": ["Unresolved items, risks, or follow-ups"],
  "files_changed": ["Key files created or modified"],
  "narrative": "4-10 paragraphs, chronological, technically dense, including dead \
ends, pivots, and what changed the developer's mind."
}}

When in doubt, include more technical detail rather than less.

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
    is_error: bool = False  # transient failure — should retry later
    total_turns: int = 0
    tool_actions: list = field(default_factory=list)
    turn_log: str = ""  # one-liner-per-turn chronological log


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


def _extract_json(text: str) -> dict | None:
    """Try multiple strategies to extract valid JSON from model output."""
    text = text.strip()
    if not text:
        return None

    # Strategy 1: Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from ```json ... ``` blocks
    if "```json" in text:
        try:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return json.loads(text[start:end].strip())
        except (ValueError, json.JSONDecodeError):
            pass
    if "```" in text:
        try:
            start = text.index("```") + 3
            end = text.index("```", start)
            return json.loads(text[start:end].strip())
        except (ValueError, json.JSONDecodeError):
            pass

    # Strategy 3: Find the outermost { ... } pair (handles trailing text after JSON)
    first_brace = text.find("{")
    if first_brace >= 0:
        # Walk from the end to find the matching closing brace
        depth = 0
        in_string = False
        escape = False
        last_brace = -1
        for i in range(first_brace, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    last_brace = i
                    # Don't break — find the LAST balanced closing brace
        if last_brace > first_brace:
            try:
                return json.loads(text[first_brace:last_brace + 1])
            except json.JSONDecodeError:
                pass

    # Strategy 4: Truncated JSON — find last complete field by trimming from end
    if first_brace >= 0:
        candidate = text[first_brace:]
        # Try progressively shorter substrings ending at "}" or "]}"
        for end_marker in ["}}", "]}", "}\n}", "\"}"]:
            idx = candidate.rfind(end_marker)
            if idx > 0:
                attempt = candidate[:idx + len(end_marker)]
                # Close any remaining open braces
                open_braces = attempt.count("{") - attempt.count("}")
                attempt += "}" * max(0, open_braces)
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    continue

    return None


def _parse_claude_response(stdout: str, entry: ChronicleEntry) -> ChronicleEntry:
    """Parse claude -p JSON output into a ChronicleEntry."""
    # Parse the outer JSON wrapper from claude -p --output-format json
    try:
        outer = json.loads(stdout)
        raw_text = outer.get("result", stdout)
    except json.JSONDecodeError:
        raw_text = stdout

    raw_text = raw_text.strip()

    if raw_text == "NO_DECISIONS" or "NO_DECISIONS" in raw_text[:50]:
        entry.is_empty = True
        return entry

    data = _extract_json(raw_text)

    # Validate that extracted JSON looks like a real chronicle response, not
    # garbage parsed from an HTML error page or other non-chronicle content.
    if data and isinstance(data, dict) and ("title" in data or "summary" in data or "decisions" in data):
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
    else:
        print(f"[chronicle] JSON extraction failed, using raw text", file=sys.stderr)
        entry.title = "Session summary (unstructured)"
        entry.narrative = raw_text[:10000]

    return entry


async def async_summarize_session(digest: SessionDigest) -> ChronicleEntry:
    """One-shot summarization via claude -p. No session persistence — clean resume picker.

    Uses --no-session-persistence so observer calls never appear in the user's
    session list. Cross-referencing is done by feeding recent chronicle titles
    as context in the prompt.
    """
    config = load_config()
    model = config.get("model", "opus")

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

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=300
        )

        if proc.returncode != 0:
            # Error details are often in stdout (JSON with is_error:true), not stderr
            err_msg = stderr.decode()[:200]
            if not err_msg:
                try:
                    err_data = json.loads(stdout.decode())
                    err_msg = err_data.get("result", "unknown error")[:200]
                except Exception:
                    err_msg = stdout.decode()[:200] or "unknown error"
            print(f"[chronicle] claude -p failed: {err_msg}", file=sys.stderr)
            entry.is_error = True
            return entry

        entry = _parse_claude_response(stdout.decode(), entry)

    except asyncio.TimeoutError:
        print("[chronicle] claude -p timed out, killing subprocess", file=sys.stderr)
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        entry.is_error = True
    except Exception as e:
        print(f"[chronicle] summarization error: {e}", file=sys.stderr)
        entry.is_error = True

    return entry


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
