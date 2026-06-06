"""Meta-test: the drift gates actually CATCH drift (not rubber-stamps).

Mirrors the calc diagnostic's oracle meta-test — prove the verifier erupts on
deliberately wrong CODEMAP entries before trusting it to bless real docs.
"""
from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    CodeMap,
    Entry,
    check_complete,
    check_grounded,
    check_signatures,
)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def func(a, b):\n    return a\n\n\n"
        "class C:\n    def m(self, x):\n        return x\n"
    )
    return tmp_path


def _correct_cm() -> CodeMap:
    return CodeMap((
        Entry("pkg.mod", "module", "src/pkg/mod.py", 1),
        Entry("pkg.mod.func", "function", "src/pkg/mod.py", 1, "func(a, b)"),
        Entry("pkg.mod.C", "class", "src/pkg/mod.py", 5),
        Entry("pkg.mod.C.m", "method", "src/pkg/mod.py", 6, "m(self, x)"),
    ))


def test_all_gates_clean_on_correct_codemap(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _correct_cm()
    assert check_grounded(repo, cm) == []
    assert check_signatures(repo, cm) == []
    assert check_complete(repo, cm) == []


def _swap(cm: CodeMap, entry_id: str, new: Entry) -> CodeMap:
    return CodeMap(tuple(new if e.id == entry_id else e for e in cm.entries))


def test_grounded_erupts_on_wrong_file(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _swap(_correct_cm(), "pkg.mod.func",
               Entry("pkg.mod.func", "function", "src/pkg/ghost.py", 1, "func(a, b)"))
    assert check_grounded(repo, cm)  # non-empty


def test_grounded_erupts_on_invented_symbol(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = CodeMap(_correct_cm().entries + (
        Entry("pkg.mod.invented", "function", "src/pkg/mod.py", 1, "invented()"),))
    assert any("invented" in v for v in check_grounded(repo, cm))


def test_signature_erupts_on_renamed_param(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = _swap(_correct_cm(), "pkg.mod.func",
               Entry("pkg.mod.func", "function", "src/pkg/mod.py", 1, "func(a, ZZZ)"))
    assert any("pkg.mod.func" in v for v in check_signatures(repo, cm))


def test_complete_erupts_on_dropped_entry(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = CodeMap(tuple(e for e in _correct_cm().entries if e.id != "pkg.mod.C.m"))
    assert any("pkg.mod.C.m" in v for v in check_complete(repo, cm))
