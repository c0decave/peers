from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    CodeMap,
    Entry,
    check_signatures,
    parse_signature_params,
)


def test_parse_signature_params_named_and_unnamed():
    assert parse_signature_params("func(a, b)") == ["a", "b"]
    assert parse_signature_params("run(self, x, *args, **kw)") == [
        "self", "x", "*args", "**kw"]
    assert parse_signature_params("(a, b)") == ["a", "b"]  # name optional
    assert parse_signature_params("!!not a sig!!") is None


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def func(a, b):\n    return a\n\n\nclass C:\n    def m(self, x):\n        return x\n"
    )
    return tmp_path


def test_signature_match_clean(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = CodeMap((
        Entry("mod.func", "function", "src/mod.py", 1, "func(a, b)"),
        Entry("mod.C.m", "method", "src/mod.py", 6, "m(self, x)"),
    ))
    assert check_signatures(repo, cm) == []


def test_signature_match_flags_renamed_param(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = CodeMap((Entry("mod.func", "function", "src/mod.py", 1, "func(a, c)"),))
    v = check_signatures(repo, cm)
    assert len(v) == 1 and "mod.func" in v[0]


def test_signature_match_flags_extra_param(tmp_path: Path):
    repo = _repo(tmp_path)
    cm = CodeMap((Entry("mod.func", "function", "src/mod.py", 1, "func(a, b, d)"),))
    assert len(check_signatures(repo, cm)) == 1


def test_signature_match_ignores_absent_symbol(tmp_path: Path):
    # grounded handles absence; signature-match must not double-report
    repo = _repo(tmp_path)
    cm = CodeMap((Entry("mod.ghost", "function", "src/mod.py", 1, "ghost()"),))
    assert check_signatures(repo, cm) == []
