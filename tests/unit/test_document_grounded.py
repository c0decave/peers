from __future__ import annotations

from pathlib import Path

from peers.codemap import CodeMap, Entry, check_grounded


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def func(a, b):\n    return a\n\n\nclass C:\n    def m(self, x):\n        return x\n"
    )
    return tmp_path


def _cm(*entries: Entry) -> CodeMap:
    return CodeMap(entries=tuple(entries))


def test_grounded_clean_for_real_symbols(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _cm(
        Entry("pkg.mod", "module", "src/pkg/mod.py", 1),
        Entry("pkg.mod.func", "function", "src/pkg/mod.py", 1, "func(a, b)"),
        Entry("pkg.mod.C", "class", "src/pkg/mod.py", 5),
        Entry("pkg.mod.C.m", "method", "src/pkg/mod.py", 6, "m(self, x)"),
    )
    assert check_grounded(repo, cm) == []


def test_grounded_flags_nonexistent_symbol(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _cm(Entry("pkg.mod.ghost", "function", "src/pkg/mod.py", 1, "ghost()"))
    v = check_grounded(repo, cm)
    assert len(v) == 1 and "ghost" in v[0]


def test_grounded_flags_wrong_file(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _cm(Entry("pkg.mod.func", "function", "src/pkg/other.py", 1, "func(a, b)"))
    v = check_grounded(repo, cm)
    assert len(v) == 1 and "other.py" in v[0]


def test_grounded_flags_kind_mismatch(tmp_path: Path):
    repo = _repo(tmp_path)
    # `func` is a function, but the map claims it is a class
    cm = _cm(Entry("pkg.mod.func", "class", "src/pkg/mod.py", 1))
    v = check_grounded(repo, cm)
    assert len(v) == 1
