"""Shared fixtures for the Stage-4 baseline-builder + landing-contract unit suite.

Not a test module (underscore prefix) — imported as
``from tests.unit._baseline_landing_helpers import ...``.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig
from peers.spine.baseline import CandidateBaseline


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a],
                          capture_output=True, text=True, check=True).stdout

def _run(tmp_path, *, with_runner=False):
    """A develop ModeRun over ``tmp_path``. By DEFAULT no pytest marker is seeded,
    so the bar is ABSENT (the Stage-4 build path). Pass with_runner=True to seed a
    present-bar repo (the 'reused'/already-green path)."""
    if with_runner:
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    return ModeRun(tool=tmp_path,
                   op_config=OpConfig.from_dict({"mode": "develop"}),
                   ledger_path=tmp_path / "run.jsonl", mode_run="r1")

def _attested_repo(p, peer="claude"):
    # TWO commits — `base` is REQUIRED: attest_commits no-ops on a falsy since_sha.
    from peers import attest
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = _git(p, "rev-parse", "HEAD").strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = _git(p, "rev-parse", "HEAD").strip()
    attest.attest_commits(p, peer, base, sha)        # HEAD attested to `peer`
    # HONEST-01: the landing tests build the contract with branch="feat/x"; the
    # convergence gate now anchors attest-reachability on that branch, so the
    # attested commit must live ON it (mirrors a real run, whose converged commit
    # is on its run branch). Point feat/x at the attested HEAD.
    _git(p, "branch", "feat/x")
    return sha

def _repo_with_commit(p):        # a REAL commit but NO peers-attest note (the negative e2e)
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    return _git(p, "rev-parse", "HEAD").strip()

def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

# ---- Trivial BaselineAuthor port fakes (derived from the Task-1 Protocol) ----
class _NullAuthor:
    """Cannot author any characterization test -> the builder is uncharacterizable."""
    def author(self, repo, bar):
        return None

class _FileAuthor:
    """Writes a REAL characterization test file under ``repo`` and returns a
    CandidateBaseline pointing at it with the command that runs it. The file is
    the out-of-band artifact the `file` witness re-hashes."""
    def __init__(self, name="test_characterization.py",
                 body="def test_current_behavior():\n    assert 1 == 1\n",
                 command="python3 -m pytest test_characterization.py"):
        self.name = name
        self.body = body
        self.command = command
    def author(self, repo, bar):
        path = Path(repo) / self.name
        path.write_text(self.body)
        return CandidateBaseline(path=str(path), command=self.command)

# A BaselineAuthor that writes a file but whose authored tests will be RED:
# pair it with run_tests=lambda cmd: (1, "1 failed") so the builder is uncharacterizable
# EVEN THOUGH a file was written (locks "green-only upgrade").
_RedAuthor = _FileAuthor   # same shape; the redness comes from the injected runner

# ---- run_tests fakes (the existing RunTests injected-runner shape) ----
def _green(cmd):  return (0, "1 passed")
def _red(cmd):    return (1, "1 failed")
def _norun(cmd):  return None
