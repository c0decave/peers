"""R5: a real research ``Committer`` adapter (``ReportCommitter``). Commits the
report the Synthesizer wrote, WITHOUT modifying its content, and attests it.

Honesty seam: it re-verifies the on-disk report sha256 against the
ReportArtifact.content_hash before committing (the synthesizer is the sole
writer — drift => fail CLOSED), commits ONLY the report file (never `-A`), and
attributes the commit via the substrate peers-attest note.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from peers.research.adapters import ReportCommitter
from peers.research.ports import Committer, ReportArtifact


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed").write_text("x", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _write_report(repo: Path, body: str = "# Research\n\nfindings here\n") -> ReportArtifact:
    path = repo / "RESEARCH.md"
    path.write_text(body, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ReportArtifact(path=str(path), content_hash=digest, confirmed_ids=["c1"])


# --- happy path ---------------------------------------------------------------
def test_happy_commits_and_attests_the_report(tmp_path: Path) -> None:
    from peers.attest import attested_peer

    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    art = _write_report(repo)
    res = ReportCommitter(attest_peer="research").implement(art, repo)
    assert isinstance(ReportCommitter(), Committer)
    assert res.ok is True
    assert res.head_sha and res.head_sha != base
    assert attested_peer(repo, res.head_sha) == "research"
    # the report is in the commit
    assert "RESEARCH.md" in _git(repo, "show", "--name-only", "--format=", res.head_sha)


# --- sad path -----------------------------------------------------------------
def test_sad_drifted_report_content_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    art = _write_report(repo)
    # someone (not the synthesizer) rewrote the file after synthesis:
    (repo / "RESEARCH.md").write_text("# Tampered\n", encoding="utf-8")
    res = ReportCommitter().implement(art, repo)
    assert res.ok is False
    assert _git(repo, "rev-parse", "HEAD") == base   # nothing committed


def test_sad_symlinked_report_is_rejected(tmp_path: Path) -> None:
    # HS-R4: the committer must not follow a symlinked report.path.
    import os

    repo = _repo(tmp_path)
    real = repo / "real_report.md"
    real.write_text("# Research\n\nbody\n", encoding="utf-8")
    link = repo / "RESEARCH.md"
    os.symlink(real, link)
    art = ReportArtifact(path=str(link),
                         content_hash=hashlib.sha256(real.read_bytes()).hexdigest(),
                         confirmed_ids=[])
    res = ReportCommitter().implement(art, repo)
    assert res.ok is False


def test_sad_missing_report_file_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    art = ReportArtifact(path=str(repo / "RESEARCH.md"), content_hash="deadbeef",
                         confirmed_ids=[])
    res = ReportCommitter().implement(art, repo)
    assert res.ok is False


# --- edge ---------------------------------------------------------------------
def test_edge_commits_only_the_report_not_other_dirty_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    art = _write_report(repo)
    (repo / "unrelated.py").write_text("junk = 1\n", encoding="utf-8")  # dirty, unstaged
    res = ReportCommitter().implement(art, repo)
    assert res.ok is True
    files = _git(repo, "show", "--name-only", "--format=", res.head_sha)
    assert "RESEARCH.md" in files
    assert "unrelated.py" not in files   # never swept into the commit


def test_sad_pre_staged_file_is_not_swept_into_the_report_commit(tmp_path: Path) -> None:
    # RC-01 (HIGH): a bare `git commit` would sweep the whole staged index. A
    # pre-STAGED unrelated file must NOT ride along in the attested report commit.
    repo = _repo(tmp_path)
    art = _write_report(repo)
    (repo / "unrelated.py").write_text("junk = 1\n", encoding="utf-8")
    _git(repo, "add", "unrelated.py")   # PRE-STAGED in the index
    res = ReportCommitter().implement(art, repo)
    assert res.ok is True
    files = _git(repo, "show", "--name-only", "--format=", res.head_sha)
    assert "RESEARCH.md" in files
    assert "unrelated.py" not in files   # the pre-staged file did NOT ride along
