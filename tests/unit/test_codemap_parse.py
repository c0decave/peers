from __future__ import annotations

from pathlib import Path

import pytest

from peers.codemap import CodeMapError, Entry, parse_codemap


def test_parse_valid_codemap(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text(
        "entries:\n"
        "  - id: pkg.mod.func\n"
        "    kind: function\n"
        "    file: src/pkg/mod.py\n"
        "    line: 10\n"
        '    signature: "func(a, b)"\n'
        "    summary: does a thing\n"
    )
    cm = parse_codemap(p)
    assert len(cm.entries) == 1
    e = cm.entries[0]
    assert isinstance(e, Entry)
    assert e.id == "pkg.mod.func"
    assert e.kind == "function"
    assert e.file == "src/pkg/mod.py"
    assert e.line == 10
    assert e.signature == "func(a, b)"
    assert e.name == "func"  # last dotted segment


def test_missing_required_field_raises(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text("entries:\n  - id: x\n    kind: function\n")  # no file/line
    with pytest.raises(CodeMapError):
        parse_codemap(p)


def test_bad_kind_raises(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text(
        "entries:\n  - id: x\n    kind: gizmo\n    file: a.py\n    line: 1\n"
    )
    with pytest.raises(CodeMapError):
        parse_codemap(p)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(CodeMapError):
        parse_codemap(tmp_path / "nope.yaml")


def test_entries_must_be_list(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text("entries: not-a-list\n")
    with pytest.raises(CodeMapError):
        parse_codemap(p)


# line field must be a real int, not a coercible value.
# YAML `true` ⇒ Python True; `int(True) == 1` silently passes the
# coercion-based validator and a symbol declared at line `true` then
# matches AST line 1 in grounded/signature gates. Numeric strings and
# floats have the same lossy-coercion problem.
def test_line_bool_rejected(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text(
        "entries:\n"
        "  - id: pkg.mod.x\n"
        "    kind: function\n"
        "    file: a.py\n"
        "    line: true\n"
    )
    with pytest.raises(CodeMapError, match="line must"):
        parse_codemap(p)


def test_line_numeric_string_rejected(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text(
        "entries:\n"
        "  - id: pkg.mod.x\n"
        "    kind: function\n"
        "    file: a.py\n"
        '    line: "42"\n'
    )
    with pytest.raises(CodeMapError, match="line must"):
        parse_codemap(p)


def test_line_float_rejected(tmp_path: Path):
    p = tmp_path / "CODEMAP.yaml"
    p.write_text(
        "entries:\n"
        "  - id: pkg.mod.x\n"
        "    kind: function\n"
        "    file: a.py\n"
        "    line: 1.5\n"
    )
    with pytest.raises(CodeMapError, match="line must"):
        parse_codemap(p)
