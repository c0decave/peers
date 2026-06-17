from __future__ import annotations

import pytest

from peers.codemap import index_module, SymbolInfo

SRC = """
def top(a, b=1, *args, **kw):
    return a


class C:
    def m(self, x):
        return x

    def _private(self):
        return 1
"""


def test_index_finds_function_and_method(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(SRC)
    idx = index_module(f)
    assert idx is not None
    assert "top" in idx and idx["top"].kind == "function"
    assert idx["top"].params == ["a", "b", "*args", "**kw"]
    assert "C" in idx and idx["C"].kind == "class"
    assert "C.m" in idx and idx["C.m"].kind == "method"
    assert idx["C.m"].params == ["self", "x"]
    assert "C._private" in idx  # index everything; "public" filtering is separate
    assert isinstance(idx["top"], SymbolInfo)
    assert idx["top"].lineno == 2  # 1-based, after the leading newline


def test_index_syntax_error_returns_none(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def (:\n")
    assert index_module(f) is None


def test_index_missing_file_returns_none(tmp_path):
    assert index_module(tmp_path / "nope.py") is None


def test_index_refuses_symlinked_source_file(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def leaked(secret):\n    return secret\n", encoding="utf-8")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable for this platform: {exc}")

    assert index_module(link) is None
