"""The document-mode drift gates are path-aware: they default to a repo-root
`CODEMAP.yaml` (the committed document-mode deliverable) but accept an explicit
path so the SAME gates can validate a map stored elsewhere — e.g. the free
primer's `.peers/CODEMAP.yaml`. This is what un-orphans the primer's machine map.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from peers.codemap_gen import CODEMAP_FILE, build_structural_codemap, serialize_codemap

_CHECKS = (Path(__file__).resolve().parents[2]
           / "src/peers/templates/modes/document/checks")


def _load(name: str):
    """Load a check script by file path (the checks/ dir has no __init__.py;
    production invokes them via `run-check` as subprocesses)."""
    spec = importlib.util.spec_from_file_location(f"_doc_check_{name}",
                                                  _CHECKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grounded = _load("grounded")
signature_match = _load("signature_match")
complete = _load("complete")


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def func(a, b):\n    return a\n\n\nclass C:\n    def m(self, x):\n        return x\n",
        encoding="utf-8")
    return tmp_path


def _write_codemap(repo: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(serialize_codemap(build_structural_codemap(repo)),
                    encoding="utf-8")
    return dest


def test_gates_validate_codemap_at_custom_path(tmp_path):
    # CODEMAP lives in .peers/, NOT at repo root — gates pointed there are clean.
    repo = _repo(tmp_path)
    cm_path = _write_codemap(repo, repo / ".peers" / CODEMAP_FILE)
    assert grounded.main(str(repo), str(cm_path)) == 0
    assert signature_match.main(str(repo), str(cm_path)) == 0
    assert complete.main(str(repo), str(cm_path)) == 0


def test_gate_default_path_is_repo_root(tmp_path):
    # No path arg → still reads <project_dir>/CODEMAP.yaml (document-mode default).
    repo = _repo(tmp_path)
    _write_codemap(repo, repo / "CODEMAP.yaml")
    assert grounded.main(str(repo)) == 0
    assert complete.main(str(repo)) == 0


def test_gate_flags_drift_at_custom_path(tmp_path):
    # A deliberately fabricated entry at the custom path must still erupt —
    # the path-awareness doesn't weaken the gate.
    repo = _repo(tmp_path)
    bad = repo / ".peers" / CODEMAP_FILE
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "entries:\n"
        "- id: pkg.mod.ghost\n  kind: function\n"
        "  file: src/pkg/mod.py\n  line: 1\n  signature: ghost()\n",
        encoding="utf-8")
    assert grounded.main(str(repo), str(bad)) == 1


def test_signature_gate_flags_drift_at_custom_path(tmp_path):
    # Real symbol is `func(a, b)`; a wrong-arity signature at the custom path
    # must still erupt — proving signature_match reads the passed path too.
    repo = _repo(tmp_path)
    bad = repo / ".peers" / CODEMAP_FILE
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "entries:\n"
        "- id: pkg.mod.func\n  kind: function\n"
        "  file: src/pkg/mod.py\n  line: 1\n  signature: func(a)\n",
        encoding="utf-8")
    assert signature_match.main(str(repo), str(bad)) == 1


def test_gate_missing_codemap_at_custom_path_fails(tmp_path):
    repo = _repo(tmp_path)
    assert grounded.main(str(repo), str(repo / ".peers" / CODEMAP_FILE)) == 1
