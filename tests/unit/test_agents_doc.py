"""Phase 2: AGENTS.md is a deterministic render of the verified CODEMAP, kept in
sync by a byte-equality gate. No LLM — every line is anchored to a CODEMAP entry.
"""
from __future__ import annotations

from peers.codemap import CodeMap, Entry
from peers.codemap_gen import check_agents_sync, render_agents_md


def _cm() -> CodeMap:
    return CodeMap((
        Entry("pkg.mod", "module", "src/pkg/mod.py", 1, summary="The mod module."),
        Entry("pkg.mod.pub", "function", "src/pkg/mod.py", 2, "pub(a, b)",
              "Does the pub thing and returns a."),
        Entry("pkg.mod.Thing", "class", "src/pkg/mod.py", 5,
              summary="Holds widget state."),
        Entry("pkg.mod.Thing.method", "method", "src/pkg/mod.py", 6,
              "method(self, x)", "Maps x to y."),
    ))


def test_render_groups_by_module_with_summaries():
    out = render_agents_md(_cm())
    assert "# AGENTS.md" in out
    assert "## pkg.mod" in out
    assert "The mod module." in out
    assert "**class Thing**" in out and "Holds widget state." in out
    assert "method(self, x)" in out and "Maps x to y." in out
    assert "pub(a, b)" in out and "Does the pub thing" in out
    # method nested (indented) under its class; function at module level
    assert "  - `method(self, x)`" in out


def test_render_is_deterministic_and_sorted():
    cm = _cm()
    assert render_agents_md(cm) == render_agents_md(cm)


def test_render_empty_codemap():
    out = render_agents_md(CodeMap(()))
    assert "AGENTS.md" in out and "empty" in out.lower()


def test_check_agents_sync_clean_after_render(tmp_path):
    cm = _cm()
    (tmp_path / "AGENTS.md").write_text(render_agents_md(cm), encoding="utf-8")
    assert check_agents_sync(tmp_path, cm) == []


def test_check_agents_sync_flags_missing(tmp_path):
    v = check_agents_sync(tmp_path, _cm())
    assert len(v) == 1 and "missing" in v[0].lower()


def test_check_agents_sync_flags_hand_edit_drift(tmp_path):
    cm = _cm()
    (tmp_path / "AGENTS.md").write_text(
        render_agents_md(cm) + "\nsneaky hand edit\n", encoding="utf-8")
    v = check_agents_sync(tmp_path, cm)
    assert len(v) == 1 and "sync" in v[0].lower()


def test_check_agents_sync_flags_crlf_corruption(tmp_path):
    # The gate is a TRUE byte comparison — CRLF line endings must NOT pass
    # (read_text would normalize them and slip past the moat).
    cm = _cm()
    rendered = render_agents_md(cm)
    (tmp_path / "AGENTS.md").write_bytes(
        rendered.replace("\n", "\r\n").encode("utf-8"))
    assert len(check_agents_sync(tmp_path, cm)) == 1


def test_check_agents_sync_flags_stale_after_summary_change(tmp_path):
    cm = _cm()
    (tmp_path / "AGENTS.md").write_text(render_agents_md(cm), encoding="utf-8")
    changed = CodeMap(tuple(
        Entry(e.id, e.kind, e.file, e.line, e.signature,
              "DIFFERENT" if e.id == "pkg.mod.pub" else e.summary)
        for e in cm.entries))
    assert len(check_agents_sync(tmp_path, changed)) == 1


# ---- CLI: peers agents-doc ----

def _write_codemap(tmp_path):
    from peers.codemap_gen import serialize_codemap
    (tmp_path / "CODEMAP.yaml").write_text(serialize_codemap(_cm()),
                                           encoding="utf-8")


def test_cmd_agents_doc_writes_gate_clean(tmp_path):
    from peers.cli import cmd_agents_doc
    _write_codemap(tmp_path)
    assert cmd_agents_doc(tmp_path) == 0
    assert (tmp_path / "AGENTS.md").is_file()
    assert cmd_agents_doc(tmp_path, check=True) == 0  # now in sync


def test_cmd_agents_doc_check_fails_when_missing(tmp_path):
    from peers.cli import cmd_agents_doc
    _write_codemap(tmp_path)
    assert cmd_agents_doc(tmp_path, check=True) == 1  # AGENTS.md not written yet


def test_cmd_agents_doc_fails_without_codemap(tmp_path):
    from peers.cli import cmd_agents_doc
    assert cmd_agents_doc(tmp_path) == 1


def test_agents_doc_parser_has_check_flag():
    from peers.cli import build_parser
    args = build_parser().parse_args(["agents-doc", "--check"])
    assert args.cmd == "agents-doc" and args.check is True
    assert build_parser().parse_args(["agents-doc"]).check is False
