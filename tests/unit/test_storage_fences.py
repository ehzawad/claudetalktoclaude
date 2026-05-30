"""Regression tests for fence-aware marker handling in storage.py.

Chronicle stores tool output untruncated, and its own sessions routinely
cat/grep chronicle.md and storage.py — so verbatim payloads legitimately contain
the structural marker strings (<!-- session: -->, <!-- prompts -->, timeline
rows). Marker/heading operations must skip fenced regions or they corrupt the
document on the next splice/rebuild/remove.
"""
from chronicle.storage import (
    _demote_headings, _fenced_spans, _unfenced_index,
    _remove_session_entry, _splice_detail, _extract_user_prompts_details,
    _PROMPTS_MARKER,
)


def test_neutralizes_llm_details_tags_in_summary():
    # When the LLM summarizes a session ABOUT collapsible markdown, its summary
    # text contains literal <details>/<summary> tags that would otherwise open a
    # fold swallowing the rest of the document (or close a structural block early).
    from chronicle.summarizer import _neutralize_structural
    t = ("Layout = a collapsed <details> per session; rejected per-turn "
         "<details open> and the </details> alternative; <summary>x</summary>")
    out = _neutralize_structural(t)
    for tag in ("<details>", "<details open>", "</details>", "<summary>", "</summary>"):
        assert tag not in out
    assert "&lt;details&gt;" in out and "&lt;/details&gt;" in out
    # ordinary markdown / angle brackets that are NOT details/summary survive
    assert _neutralize_structural("a < b and **bold** and `<code>`") == \
        "a < b and **bold** and `<code>`"
    edge = _neutralize_structural(
        "A <DETAILS\nopen> and <summary\n> survive; < details > is text"
    )
    assert "<DETAILS" not in edge
    assert "<summary" not in edge
    assert "< details >" in edge


def test_neutralizes_llm_echoed_structural_markers():
    # The real truncation bug: summarizing a session ABOUT Chronicle makes the
    # LLM write '<!-- prompts -->' / '<!-- session: -->' into its UNFENCED summary
    # prose. Left raw, rebuild_prompts_section matches the echoed marker and
    # truncates chronicle.md. Neutralize the '<!--' so it's literal text.
    from chronicle.summarizer import _neutralize_structural
    t = ("Fixed rebuild_prompts_section to locate the <!-- prompts --> marker "
         "and bounded the <!-- session:abc --> scan; also <!-- /timeline -->.")
    out = _neutralize_structural(t)
    assert "<!--" not in out
    assert "&lt;!-- prompts -->" in out and "&lt;!-- session:abc -->" in out


def test_entry_markdown_neutralizes_only_summary_not_raw_blocks():
    # The joined LLM summary fields are neutralized, but Chronicle's own raw
    # <details> structures are appended afterward and must remain parseable.
    from chronicle.extractor import UserPrompt
    from chronicle.summarizer import ChronicleEntry, entry_to_session_markdown
    entry = ChronicleEntry(
        session_id="scope-test",
        project_path="/tmp/p",
        project_slug="-tmp-p",
        start_time="2026-05-29T10:00:00Z",
        end_time="2026-05-29T10:30:00Z",
        git_branch="main",
        user_prompts=[UserPrompt(
            text="literal <!-- prompt marker --> and </details>",
            timestamp="2026-05-29T10:00:00Z",
            uuid="u",
        )],
        title="T",
        summary="LLM echoed <!-- prompts --> and <details><summary>x</summary>",
        narrative="N",
        turn_log="### Turn index\n\nraw Chronicle <details><summary>kept</summary></details>",
    )
    md = entry_to_session_markdown(entry)
    summary_part = md.split("## Turn-by-turn log", 1)[0]
    assert "<!-- prompts -->" not in summary_part
    assert "<details><summary>x</summary>" not in summary_part
    assert "&lt;!-- prompts -->" in summary_part
    assert "&lt;details&gt;&lt;summary&gt;x&lt;/summary&gt;" in summary_part
    assert "raw Chronicle <details><summary>kept</summary></details>" in md
    assert "<details><summary>User prompts (verbatim)</summary>" in md
    assert "&lt;!-- prompt marker --&gt; and &lt;/details&gt;" in md


def test_prompt_details_extractor_ignores_fenced_lookalike():
    content = (
        "# Session\n\n"
        "### Full chronological log\n\n"
        "<details><summary>Full chronological log</summary>\n\n"
        "`````\n"
        "<details><summary>User prompts (verbatim)</summary>\n\n"
        "**Prompt 99** (1999-01-01 00:00:00):\n"
        "> FAKE FROM CAT\n\n"
        "</details>\n"
        "`````\n\n"
        "</details>\n\n"
        "---\n\n"
        "<details><summary>User prompts (verbatim)</summary>\n\n"
        "**Prompt 1** (2026-05-29 10:00:00):\n"
        "> REAL PROMPT\n\n"
        "</details>\n"
    )
    prompts_text = _extract_user_prompts_details(content)
    assert prompts_text is not None
    assert "REAL PROMPT" in prompts_text
    assert "FAKE FROM CAT" not in prompts_text


def test_remove_session_entry_keeps_fenced_timeline_rows():
    # Session A's verbatim output contains a fenced `cat chronicle.md` whose copy
    # of the timeline table includes B's row. Removing A must delete only A's REAL
    # (unfenced) table row, never mutate the fenced copy.
    a = "aaaaaaaa-1111-1111-1111-111111111111"
    b = "bbbbbbbb-2222-2222-2222-222222222222"
    fenced_copy = (
        "`````\n"
        "| Date | Session | Decisions | Summary |\n"
        "|------|---------|-----------|---------|\n"
        f"| 2026-01-02 | [B](sessions/2026-01-02_1000_{b[:8]}_b.md) | 1 | s |\n"
        "<!-- /timeline -->\n"
        "`````\n"
    )
    # The fenced copy lives in session B's detail (which survives); removing A
    # must not mutate B's fenced verbatim table.
    content = (
        "# Chronicle: x\n\n"
        "| Date | Session | Decisions | Summary |\n"
        "|------|---------|-----------|---------|\n"
        f"| 2026-01-01 | [A](sessions/2026-01-01_1000_{a[:8]}_a.md) | 1 | s |\n"
        f"| 2026-01-02 | [B](sessions/2026-01-02_1000_{b[:8]}_b.md) | 1 | s |\n"
        "<!-- /timeline -->\n\n"
        "<!-- details -->\n\n"
        f"## Session A\n<!-- session:{a} -->\n\nbody A\n---\n\n"
        f"## Session B\n<!-- session:{b} -->\n\n" + fenced_copy + "\nbody B\n---\n\n"
    )
    out = _remove_session_entry(content, f"<!-- session:{a} -->")
    # A's real row removed; B's section + its FENCED copy preserved byte-for-byte
    assert f"[A](sessions/2026-01-01_1000_{a[:8]}_a.md)" not in out
    assert fenced_copy in out
    assert "## Session B" in out and "body B" in out


def test_timeline_row_title_normalized_to_single_safe_row():
    from types import SimpleNamespace
    from chronicle.storage import _timeline_row
    e = SimpleNamespace(
        start_time="2026-05-29T10:00:00Z", session_id="aaaaaaaa-1",
        title="bad\n# injected heading\n| 2099 | [x](sessions/y.md) | 9 | f |",
        summary="ok", decisions=[])
    row = _timeline_row(e, "s.md")
    assert "\n" not in row                 # no injected extra lines/headings
    assert row.count("|") == 5             # exactly one table row (4 cells)
    assert "# injected heading" in row     # collapsed to inline text, not a heading line


def test_demote_preserves_hashes_inside_dynamic_fences():
    md = (
        "# Title\n\n"
        "`````\n"          # 5-backtick opener
        "```\n"            # inner 3-backtick run (must NOT toggle/close)
        "# keep me literal\n"
        "```\n"
        "`````\n"          # real close
        "## Real heading\n"
    )
    out = _demote_headings(md)
    assert "# keep me literal" in out          # NOT demoted (inside fence)
    assert "## keep me literal" not in out
    assert "## Title" in out                   # real heading demoted
    assert "### Real heading" in out


def test_fenced_spans_and_unfenced_index():
    text = (
        "real header\n"
        "`````\n"
        "<!-- prompts -->\n"          # fenced literal (e.g. cat of storage.py)
        "`````\n"
        "tail\n"
        "<!-- prompts -->\n"          # the REAL EOF block marker
    )
    spans = _fenced_spans(text)
    # first occurrence is inside a fence -> not returned
    assert _unfenced_index(text, _PROMPTS_MARKER) == text.rfind(_PROMPTS_MARKER)
    assert _unfenced_index(text, _PROMPTS_MARKER, last=True) == text.rfind(_PROMPTS_MARKER)
    # the fenced one is inside a span
    fenced_at = text.find(_PROMPTS_MARKER)
    assert any(s <= fenced_at < e for s, e in spans)


def test_splice_detail_ignores_fenced_prompts_marker():
    # A session detail whose verbatim output contains a fenced <!-- prompts -->,
    # plus the REAL prompts block at EOF.
    existing = (
        "# Chronicle: x\n\n"
        "## Session A\n<!-- session:aaaa -->\n\n"
        "`````\n"
        "grep result: _PROMPTS_MARKER = \"<!-- prompts -->\"\n"   # fenced literal
        "`````\n\n"
        "<!-- prompts -->\n\n"
        "## All User Prompts (Chronological)\n"
    )
    detail = "## Session B\n<!-- session:bbbb -->\n\nbody B\n---\n\n"
    out = _splice_detail(existing, detail)
    # B spliced BEFORE the real EOF prompts block, not at the fenced marker
    assert "## Session B" in out
    assert out.index("## Session B") > out.index("grep result")   # after session A's fenced block
    assert out.index("## Session B") < out.rindex("<!-- prompts -->")  # before the real EOF marker
    assert "## All User Prompts" in out                            # EOF block intact


def test_remove_session_entry_ignores_fenced_session_marker():
    # Real session filenames embed the 8-char short id (session_id[:8]).
    a = "aaaaaaaa-1111-1111-1111-111111111111"
    b = "bbbbbbbb-2222-2222-2222-222222222222"
    content = (
        "# Chronicle: x\n\n"
        "| Date | Session | Decisions | Summary |\n"
        "|------|---------|-----------|---------|\n"
        f"| 2026-01-01 | [A](sessions/2026-01-01_1000_{a[:8]}_a.md) | 1 | s |\n"
        f"| 2026-01-02 | [B](sessions/2026-01-02_1000_{b[:8]}_b.md) | 1 | s |\n"
        "<!-- /timeline -->\n\n"
        "<!-- details -->\n\n"
        f"## Session A\n<!-- session:{a} -->\n\n"
        "`````\n"
        f"cat chronicle.md -> <!-- session:{b} -->\n"   # fenced lookalike of B's marker
        "`````\n\n"
        f"body A\n---\n\n"
        f"## Session B\n<!-- session:{b} -->\n\nbody B\n---\n\n"
    )
    out = _remove_session_entry(content, f"<!-- session:{a} -->")
    # Session B must SURVIVE (the fenced lookalike must not bound A's section early
    # nor get B's section removed).
    assert "## Session B" in out
    assert "body B" in out
    assert "## Session A" not in out          # A removed
    # B's timeline row kept; A's row removed
    assert f"[B](sessions/2026-01-02_1000_{b[:8]}_b.md)" in out
    assert f"[A](sessions/2026-01-01_1000_{a[:8]}_a.md)" not in out
