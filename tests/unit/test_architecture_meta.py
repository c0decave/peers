"""Meta-test: the architecture gate CATCHES drift (not a rubber-stamp). Mirrors
test_document_meta.py — prove the verifier erupts on a dangling anchor and on a
silently-omitted subsystem before trusting it to bless real prose."""
from __future__ import annotations

from pathlib import Path

from peers.codemap import CodeMap, Entry, check_architecture

_CM = CodeMap((
    Entry("pkg.alpha", "module", "src/pkg/alpha.py", 1),
    Entry("pkg.alpha.foo", "function", "src/pkg/alpha.py", 1, "foo()"),
    Entry("pkg.beta", "module", "src/pkg/beta.py", 1),
))
_GOOD = "alpha [[pkg.alpha.foo]] and beta [[pkg.beta]] cooperate.\n"


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "ARCHITECTURE.md").write_text(body, encoding="utf-8")
    return tmp_path


def test_gate_clean_on_correct_doc(tmp_path):
    assert check_architecture(_write(tmp_path, _GOOD), _CM) == []


def test_gate_erupts_on_dangling_anchor(tmp_path):
    body = _GOOD + "see [[pkg.does_not_exist]]\n"
    assert any("does_not_exist" in m
               for m in check_architecture(_write(tmp_path, body), _CM))


def test_gate_erupts_on_omitted_subsystem(tmp_path):
    body = "alpha [[pkg.alpha.foo]] only.\n"  # beta silently dropped
    assert any("beta" in m and "not covered" in m
               for m in check_architecture(_write(tmp_path, body), _CM))
