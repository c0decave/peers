"""The document-mode substrate seed: writes a structural repo-root CODEMAP.yaml
(correct structure, empty summaries) so peers add summaries rather than invent
structure. The three drift gates start green on the seed; summaries-complete
starts red and drives the build.
"""
from __future__ import annotations

from pathlib import Path

from peers.codemap import (
    check_complete,
    check_grounded,
    check_signatures,
    check_summaries,
    parse_codemap,
)
from peers.codemap_gen import REPO_CODEMAP_FILE, seed_repo_codemap


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text(
        "def pub(a, b):\n    return a\n\n\n"
        "class C:\n    def m(self, x):\n        return x\n",
        encoding="utf-8")
    return tmp_path


def test_seed_is_drift_clean_but_summaries_red(tmp_path):
    repo = _repo(tmp_path)
    status = seed_repo_codemap(repo)
    target = repo / REPO_CODEMAP_FILE
    assert target.is_file() and "wrote" in status
    cm = parse_codemap(target)
    # structure correct by construction → the 3 drift gates pass
    assert check_grounded(repo, cm) == []
    assert check_signatures(repo, cm) == []
    assert check_complete(repo, cm) == []
    # ...but nothing is documented yet → every entry fails summaries (the target)
    assert len(cm.entries) > 0
    assert len(check_summaries(cm)) == len(cm.entries)


def test_seed_is_idempotent_never_clobbers(tmp_path):
    repo = _repo(tmp_path)
    seed_repo_codemap(repo)
    target = repo / REPO_CODEMAP_FILE
    # a peer has started adding summaries — the seed must NOT overwrite it
    target.write_text(
        "entries:\n- id: pkg.mod\n  kind: module\n"
        "  file: src/pkg/mod.py\n  line: 1\n  summary: My module.\n",
        encoding="utf-8")
    status = seed_repo_codemap(repo)
    assert "skip" in status
    assert "My module." in target.read_text()
