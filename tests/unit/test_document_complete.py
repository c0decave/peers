from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    CodeMap,
    Entry,
    check_complete,
    enumerate_public_symbols,
)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def func(a):\n    return a\n\n\n"
        "def _hidden():\n    return 0\n\n\n"
        "class C:\n    def m(self):\n        return 1\n\n"
        "    def _p(self):\n        return 2\n"
    )
    return tmp_path


def test_enumerate_public_symbols(tmp_path: Path):
    pub = enumerate_public_symbols(_repo(tmp_path))
    assert pub == {"pkg.mod", "pkg.mod.func", "pkg.mod.C", "pkg.mod.C.m"}
    assert "pkg.mod._hidden" not in pub
    assert "pkg.mod.C._p" not in pub


def _full_cm() -> CodeMap:
    return CodeMap((
        Entry("pkg.mod", "module", "src/pkg/mod.py", 1),
        Entry("pkg.mod.func", "function", "src/pkg/mod.py", 1, "func(a)"),
        Entry("pkg.mod.C", "class", "src/pkg/mod.py", 9),
        Entry("pkg.mod.C.m", "method", "src/pkg/mod.py", 10, "m(self)"),
    ))


def test_complete_clean_when_all_public_documented(tmp_path: Path):
    assert check_complete(_repo(tmp_path), _full_cm()) == []


def test_complete_flags_missing_symbol(tmp_path: Path):
    repo = _repo(tmp_path)
    partial = CodeMap(tuple(e for e in _full_cm().entries if e.id != "pkg.mod.func"))
    v = check_complete(repo, partial)
    assert len(v) == 1 and "pkg.mod.func" in v[0]
