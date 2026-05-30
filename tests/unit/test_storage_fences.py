"""Regression tests for fence-aware marker handling in storage.py.

Chronicle stores tool output untruncated, and its own sessions routinely
cat/grep chronicle.md and storage.py — so verbatim payloads legitimately contain
the structural marker strings (<!-- session: -->, <!-- prompts -->, timeline
rows). Marker/heading operations must skip fenced regions or they corrupt the
document on the next splice/rebuild/remove.
"""
from chronicle.storage import (
    _demote_headings, _fenced_spans, _unfenced_index,
    _remove_session_entry, _splice_detail, _PROMPTS_MARKER,
)


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
