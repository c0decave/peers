"""The document-mode architecture seed: writes a light narrative-OUTLINE
ARCHITECTURE.md (5 sections + a subsystem checklist) so the gate starts RED
(placeholder + every subsystem uncovered) and drives the prose build."""
from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    ARCHITECTURE_FILE,
    check_architecture,
    parse_anchors,
    parse_codemap,
)
from peers.codemap_gen import (
    REPO_CODEMAP_FILE,
    seed_repo_architecture,
    seed_repo_codemap,
)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "alpha.py").write_text(
        "def foo(a):\n    return a\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "beta.py").write_text(
        "def bar(b):\n    return b\n", encoding="utf-8")
    return tmp_path


def test_arch_seed_writes_outline_and_starts_red(tmp_path):
    repo = _repo(tmp_path)
    seed_repo_codemap(repo)               # CODEMAP.yaml the gate reads
    status = seed_repo_architecture(repo)
    target = repo / ARCHITECTURE_FILE
    assert target.is_file() and "wrote" in status
    cm = parse_codemap(repo / REPO_CODEMAP_FILE)
    # the fresh outline has the placeholder AND no anchors → red
    v = check_architecture(repo, cm)
    assert any("placeholder" in m for m in v)
    assert any("not covered" in m for m in v)
    # the seed's own example must NOT auto-cover a subsystem (fenced/no anchors)
    assert parse_anchors(target.read_text(encoding="utf-8")) == []


def test_arch_seed_idempotent_never_clobbers(tmp_path):
    repo = _repo(tmp_path)
    seed_repo_architecture(repo)
    target = repo / ARCHITECTURE_FILE
    target.write_text("# my real architecture doc\n", encoding="utf-8")
    status = seed_repo_architecture(repo)
    assert "skip" in status
    assert "my real architecture doc" in target.read_text(encoding="utf-8")
