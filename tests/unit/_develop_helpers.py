"""Shared fixtures for the develop-mode unit suite (Tasks 2–5 reuse these).

Not a test module (underscore prefix) — imported as
``from tests.unit._develop_helpers import ...``.

**Bar precondition (load-bearing):** ``DevelopFrontend.prepare`` blocks ALL work
when the bar is *absent*. A bare ``tmp_path`` with only ``a.py`` classifies
absent → ``run()`` writes nothing and the asserted gate/confirmed-work/landing
rows never appear. So :func:`_run` seeds a runner marker (``pyproject.toml``) in
the SAME ``tmp_path`` as the git repo, making the bar *present* over the
attested repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig
from peers.develop.ports import AuthoredContract, Finding, ImplementResult


def _git(p: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(p), *a], capture_output=True, text=True, check=True,
    ).stdout


def _run(tmp_path: Path) -> ModeRun:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")  # present bar
    return ModeRun(
        tool=tmp_path,
        op_config=OpConfig.from_dict({"mode": "develop"}),
        ledger_path=tmp_path / "run.jsonl",
        mode_run="r1",
    )


def _attested_repo(p: Path, peer: str = "claude") -> str:
    # TWO commits — `base` is REQUIRED: attest_commits no-ops on a falsy
    # since_sha (attest.py).
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
    attest.attest_commits(p, peer, base, sha)  # HEAD attested to `peer`
    return sha


def _repo_with_commit(p: Path) -> str:
    # a REAL commit but NO peers-attest note (the negative e2e).
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    return _git(p, "rev-parse", "HEAD").strip()


def _F(fid: str = "F1", dim: str = "correctness") -> Finding:
    return Finding(id=fid, dimension=dim, severity="med", location="x:1",
                   summary="s", fix="f", fail_first="t")


# Trivial port fakes derived straight from the Task-1 Protocol signatures:
class _NullAuditor:
    def audit(self, repo, dimensions):
        return []


class _FixedAuditor:
    def __init__(self, findings, once=False):
        self.f = findings
        self.once = once
        self.n = 0

    def audit(self, repo, dimensions):
        self.n += 1
        return self.f if (not self.once or self.n == 1) else []


class _NullAuthor:
    def author(self, findings, repo):
        return None


class _FixedAuthor:
    def __init__(self, contract):
        self.c = contract

    def author(self, findings, repo):
        return self.c


class _NullImpl:
    def implement(self, contract, repo):
        return ImplementResult(ok=False, reason="null")


class _FixedImpl:
    def __init__(self, result):
        self.r = result

    def implement(self, contract, repo):
        return self.r


def _develop_fe_that_confirms_once(tmp_path, sha):
    """A frontend that confirms exactly one finding on round 1, then audits
    empty — drives to a real confirmed-work + landing, then dry rounds."""
    from peers.develop.frontend import DevelopFrontend

    return DevelopFrontend(
        _FixedAuditor([_F()], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q",
                                      findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch="feat/x")),
        dimensions=["correctness"],
        run_tests=lambda c: (0, "1 passed"),
        refuter_factory=lambda f: (lambda i: False),
    )


def _develop_fe_all_unsound(tmp_path):
    """A frontend whose every finding is refuted every round → never confirms,
    terminates on stop-on-dry with no confirmed-work."""
    from peers.develop.frontend import DevelopFrontend

    return DevelopFrontend(
        _FixedAuditor([_F()]),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q",
                                      findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha="deadbeef", branch="feat/x")),
        dimensions=["correctness"],
        run_tests=lambda c: (0, "1 passed"),
        refuter_factory=lambda f: (lambda i: True),
    )
