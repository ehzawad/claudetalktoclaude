"""Durable content-loss regression: chronicle.md must NEVER drop content when an
LLM summary echoes Chronicle's OWN structural strings.

When Chronicle summarizes a session that is *about Chronicle itself*, the model
routinely writes Chronicle's structural markers into its prose: '<!-- prompts -->',
'<!-- session:... -->', '<details>'/'</details>', bare '---' separators, and even a
fake timeline row '| ... | [x](sessions/y.md) | ... |'. Left raw and unfenced these
are indistinguishable from the real structure Chronicle injects:

  - a stray '<details>' opens a fold that swallows the rest of the document,
  - an echoed '<!-- prompts -->' makes rebuild_prompts_section() truncate the doc,
  - a bare '---' / fake timeline row can confuse _remove_session_entry bounds.

This test writes TWO sessions for the same project slug, each carrying a large
uniquely-numbered turn log (T001..T200) AND LLM fields stuffed with every one of
those structural strings, then drives the real storage pipeline
(write_session_record + append_to_chronicle + rebuild_prompts_section) and proves:

  (1) every turn id T001..T200 of BOTH sessions survives (no truncation),
  (2) fence-aware <details>/</details> are balanced and never nest negative,
  (3) each session's real '<!-- session:UUID -->' marker appears exactly once and
      the document does not end mid-fence,
  (4) rebuild_prompts_section ran without shrinking either session's detail.

If any assertion fails, that is a real remaining content-loss bug.
"""
import os
import re

import pytest

from chronicle.extractor import UserPrompt
from chronicle.summarizer import ChronicleEntry
from chronicle.storage import (
    write_session_record,
    append_to_chronicle,
    rebuild_prompts_section,
)
from chronicle.config import project_chronicle_dir


SLUG = "-Users-ehz-claudetalktoclaude"

# Every structural string Chronicle injects, which the LLM may echo into prose.
STRUCTURAL_STRINGS = [
    "<!-- prompts -->",
    "<!-- session:deadbeef -->",
    "<details>",
    "</details>",
    "<!-- /timeline -->",
    "<!-- details -->",
    "| 2026 | [x](sessions/y.md) | 1 | s |",
]


def _poison() -> str:
    """A blob of prose containing every structural string plus a bare '---' line."""
    return (
        "We fixed rebuild_prompts_section to find the <!-- prompts --> marker and "
        "bounded the <!-- session:deadbeef --> scan. The layout is a collapsed "
        "<details> per session closed by </details>; we also touched "
        "<!-- /timeline --> and the <!-- details --> anchor.\n"
        "---\n"
        "Here is a fake timeline row the model copied verbatim:\n"
        "| 2026 | [x](sessions/y.md) | 1 | s |\n"
        "...and another stray </details> just to be mean."
    )


def _turn_log(n: int, tag: str) -> str:
    """A multi-line chronological log with n uniquely-numbered turns (T001..Tnnn).

    Each turn embeds a collapsible fenced verbatim block (as the real turn log
    does), and that fenced payload deliberately contains the structural strings —
    these must be preserved because they are fenced verbatim output, not structure.
    """
    out = []
    for i in range(1, n + 1):
        tid = f"T{i:03d}"
        out.append(f"### Turn {tid} ({tag})")
        out.append("")
        out.append("<details><summary>tool output</summary>")
        out.append("")
        out.append("``````")
        out.append(f"{tid} verbatim payload that cats chronicle.md:")
        out.append("<!-- prompts -->")
        out.append("<!-- session:deadbeef -->")
        out.append("| 2026 | [x](sessions/y.md) | 1 | s |")
        out.append("```")  # shorter inner run must NOT close the 6-backtick fence
        out.append("---")
        out.append("``````")
        out.append("")
        out.append("</details>")
        out.append("")
    return "\n".join(out)


def _make_entry(session_id: str, title_tag: str, n_turns: int) -> ChronicleEntry:
    return ChronicleEntry(
        session_id=session_id,
        project_path="/Users/ehz/claudetalktoclaude",
        project_slug=SLUG,
        start_time=f"2026-05-30T0{title_tag[-1]}:00:00",
        end_time=f"2026-05-30T0{title_tag[-1]}:30:00",
        git_branch="main",
        user_prompts=[
            UserPrompt(
                text=("Please document how rebuild_prompts_section locates "
                      "<!-- prompts --> and </details>.\n" + _poison()),
                timestamp=f"2026-05-30T0{title_tag[-1]}:01:00",
                uuid=f"u-{session_id}",
            ),
        ],
        # LLM fields ALL carry structural strings:
        title=f"Chronicle structure work {title_tag} <details> </details>",
        summary=_poison(),
        narrative="Narrative: " + _poison(),
        decisions=[
            {"what": "Neutralize <details> in summary " + _poison(),
             "why": "Stop folds swallowing the doc " + _poison()},
        ],
        follow_ups=[
            {"question": "How does <!-- prompts --> truncation happen? " + _poison()},
        ],
        total_turns=n_turns,
        turn_log=_turn_log(n_turns, title_tag),
    )


# ---- fence-aware structural counting -----------------------------------------

_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")


def _unfenced_lines(text: str):
    """Yield only the lines that are OUTSIDE fenced code blocks (matching the
    same fence semantics storage.py uses: a fence closes on a bare same-char run
    of length >= the opener)."""
    fence = None
    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if m:
            token = m.group(1)
            if fence is None:
                fence = token
                continue
            if token[0] == fence[0] and len(token) >= len(fence) and line.strip() == token:
                fence = None
            continue
        if fence is None:
            yield line


def _details_balance(text: str):
    """Return (final_depth, min_depth) for real (unfenced) <details>/</details>.

    A healthy document closes every fold (final_depth == 0) and never closes one
    it did not open (min_depth >= 0)."""
    depth = 0
    min_depth = 0
    for line in _unfenced_lines(text):
        # count opens/closes on the line (HTML-escaped &lt;details&gt; is inert)
        opens = len(re.findall(r"<details(?:\s[^>]*)?>", line))
        closes = len(re.findall(r"</details>", line))
        depth += opens
        depth -= closes
        min_depth = min(min_depth, depth)
    return depth, min_depth


def _real_session_marker_count(text: str, session_id: str) -> int:
    """Count '<!-- session:UUID -->' occurrences OUTSIDE fenced regions."""
    marker = f"<!-- session:{session_id} -->"
    return sum(line.count(marker) for line in _unfenced_lines(text))


def _ends_mid_fence(text: str) -> bool:
    fence = None
    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if not m:
            continue
        token = m.group(1)
        if fence is None:
            fence = token
        elif token[0] == fence[0] and len(token) >= len(fence) and line.strip() == token:
            fence = None
    return fence is not None


@pytest.fixture
def tmp_chronicle_home(tmp_path, monkeypatch):
    """Point both HOME and CHRONICLE_HOME at a tmp dir so project_chronicle_dir
    (via chronicle_dir/projects_dir) resolves entirely under tmp."""
    home = tmp_path / "home"
    chome = tmp_path / "chron"
    home.mkdir()
    chome.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CHRONICLE_HOME", str(chome))
    return chome


def _write_session(entry):
    write_session_record(entry, SLUG)
    append_to_chronicle(entry, SLUG)
    rebuild_prompts_section(SLUG)


def test_no_content_loss_when_summary_echoes_structural_strings(tmp_chronicle_home):
    n = 200
    sid_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    sid_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    entry_a = _make_entry(sid_a, "A1", n)
    entry_b = _make_entry(sid_b, "B2", n)

    # Sanity: the env actually redirects the chronicle dir under tmp.
    cdir = project_chronicle_dir(SLUG)
    assert str(tmp_chronicle_home) in str(cdir), (cdir, tmp_chronicle_home)

    _write_session(entry_a)

    chronicle_md = cdir / "chronicle.md"
    after_a = chronicle_md.read_text()
    len_after_a = len(after_a)

    _write_session(entry_b)
    doc = chronicle_md.read_text()

    # (1) NO TRUNCATION: every turn id of BOTH sessions survives.
    for tag in ("A1", "B2"):
        for i in range(1, n + 1):
            tid = f"T{i:03d}"
            needle = f"Turn {tid} ({tag})"
            assert needle in doc, f"missing {needle!r} — content was dropped"

    # Session A's content must not have been clobbered by writing B (regression:
    # an echoed '<!-- prompts -->' or '<details>' from B's prose truncating A).
    for i in range(1, n + 1):
        assert f"Turn T{i:03d} (A1)" in doc, f"session A turn {i} lost after writing B"

    # (2) fence-aware <details> balance: every real fold closes, none under-flows.
    final_depth, min_depth = _details_balance(doc)
    assert final_depth == 0, f"unbalanced <details> (final depth {final_depth})"
    assert min_depth >= 0, f"</details> closed an unopened fold (min depth {min_depth})"

    # (3) each session's REAL marker appears exactly once; doc not mid-fence.
    assert _real_session_marker_count(doc, sid_a) == 1, "session A marker count wrong"
    assert _real_session_marker_count(doc, sid_b) == 1, "session B marker count wrong"
    # The echoed fake marker '<!-- session:deadbeef -->' must NOT be a real marker.
    assert _real_session_marker_count(doc, "deadbeef") == 0, "echoed fake marker leaked as real"
    assert not _ends_mid_fence(doc), "document ends inside an unterminated fence"

    # (4) rebuild_prompts_section ran without shrinking the session details. The
    # doc only grows (a 2nd full session + prompts section), never shrinks below
    # the single-session size, and both real prompts blocks survive.
    assert len(doc) > len_after_a, "writing session B shrank chronicle.md"
    assert doc.count("## All User Prompts (Chronological)") == 1
    # rebuild must have collected prompts from BOTH session files.
    assert "Chronicle structure work A1" in doc
    assert "Chronicle structure work B2" in doc

    # Belt-and-suspenders: the canonical EOF prompts marker exists exactly once
    # OUTSIDE fences (echoed copies inside turn-log fences must not count).
    assert sum(line.count("<!-- prompts -->") for line in _unfenced_lines(doc)) == 1
