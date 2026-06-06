"""The `summaries-complete` gate: every CODEMAP entry must carry a substantive
summary. This is the gate that drives `document` mode's build — a freshly seeded
structural CODEMAP (no summaries) fails it on every entry, and the peers
converge by writing real summaries.
"""
from __future__ import annotations

from peers.codemap import CodeMap, Entry, check_summaries


def _fn(eid: str, summary: str = "") -> Entry:
    return Entry(eid, "function", "src/m.py", 1, f"{eid.rsplit('.', 1)[-1]}()",
                 summary)


def test_clean_when_every_entry_has_a_real_summary():
    cm = CodeMap((
        Entry("m", "module", "src/m.py", 1, summary="The m module — entry point."),
        _fn("m.foo", "Applies foo to the input and returns the result."),
        Entry("m.C", "class", "src/m.py", 5, summary="Holds widget state."),
    ))
    assert check_summaries(cm) == []


def test_flags_empty_summary():
    v = check_summaries(CodeMap((_fn("m.foo", ""),)))
    assert len(v) == 1 and "m.foo" in v[0] and "missing" in v[0]


def test_flags_whitespace_only_summary():
    v = check_summaries(CodeMap((_fn("m.foo", "   \t  "),)))
    assert len(v) == 1 and "m.foo" in v[0]


def test_flags_placeholder_summary():
    for ph in ("TODO", "tbd", "...", "FIXME", "wip"):
        v = check_summaries(CodeMap((_fn("m.foo", ph),)))
        assert len(v) == 1 and "placeholder" in v[0], ph


def test_flags_too_short_summary():
    v = check_summaries(CodeMap((_fn("m.foo", "ok"),)))
    assert len(v) == 1


def test_reports_each_undocumented_entry():
    cm = CodeMap((
        _fn("m.a", "Does a real thing worth documenting."),
        _fn("m.b", ""),
        _fn("m.c", "TODO"),
    ))
    v = check_summaries(cm)
    assert len(v) == 2
    assert any("m.b" in x for x in v) and any("m.c" in x for x in v)
    assert not any("m.a" in x for x in v)
