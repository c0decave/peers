"""Shared fixtures for the Stage-5 isolation/propagation unit suite (Tasks 2-7 reuse).

Not a test module (underscore prefix) -- imported as
``from tests.unit._isolation_helpers import ...``.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from peers.spine.ledger import RunLedger
from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig
from peers.spine.worktree import RunWorkspace
# Re-exported for the Stage-7 e2e (STEP-7 imports the real provider from here, as
# it imports every other fixture). `as` is the explicit re-export form.
from peers.spine.worktree import GitWorktreeProvider as GitWorktreeProvider


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a],
                          capture_output=True, text=True, check=True).stdout


def _init_repo(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")


def _attested_repo(p, peer="claude"):
    # TWO commits -- `base` is REQUIRED: attest_commits no-ops on a falsy since_sha.
    from peers import attest
    _init_repo(p)
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = _git(p, "rev-parse", "HEAD").strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = _git(p, "rev-parse", "HEAD").strip()
    attest.attest_commits(p, peer, base, sha)        # HEAD attested to `peer`
    return sha


def _commit_on_branch(repo, branch, filename, content, peer="claude"):
    """Create `branch` at a NEW attested commit and return its full sha. When
    `branch` ALREADY exists, APPEND a new attested commit to its tip -- so a
    MULTI-commit isolated branch can be built by repeated calls (Task-3's
    wrong-base regression needs a spine touch FOLLOWED by an innocent converged
    tip on the SAME branch). The new commit alone is attested (its parent..new
    range); the earlier commit stays attested from the call that created it. The
    producer's CONVERGED artifact lives on this branch tip.

    Backward-compatible: every pre-Stage-6 caller creates a fresh branch once, so
    they take the `checkout -b` path unchanged (`exists` is False).
    """
    from peers import attest
    exists = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch}"], capture_output=True, text=True).returncode == 0
    if exists:
        _git(repo, "checkout", "-q", branch)        # re-enter to append a commit
    else:
        _git(repo, "checkout", "-q", "-b", branch)  # first use: create the branch
    base = _git(repo, "rev-parse", "HEAD").strip()  # the new commit's parent (the branch tip)
    dest = Path(repo) / filename
    dest.parent.mkdir(parents=True, exist_ok=True)  # nested paths (e.g. docs/x, src/peers/spine/x)
    dest.write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"work:{filename}")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    attest.attest_commits(repo, peer, base, sha)
    _git(repo, "checkout", "-q", "-")                # back off the branch
    return sha


def _run(tmp_path, mode="develop", *, mode_run="r1", tool=None, branch=None):
    # NOTE: the `branch=` kwarg presupposes Task 3 (which adds ModeRun.branch).
    # This fixtures module is imported by Tasks 2-7; the Task-2 isolation tests do
    # NOT call `_run`, so the module is import-safe even though it is authored
    # before Task 3 lands the field. (Tasks 4-7 that DO call `_run` run after Task 3.)
    return ModeRun(tool=tool or tmp_path,
                   op_config=OpConfig.from_dict({"mode": mode}),
                   ledger_path=(tool or tmp_path) / "run.jsonl",
                   mode_run=mode_run, branch=branch)


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _converged_branch_ledger(repo, ledger_path, mode_run, tip):
    """A CONVERGED producer ledger over a REAL attested branch-tip commit `tip`:
    run-start + an attested + git-sha-witnessed confirmed-work over `tip` + a
    terminal stop. `is_converged`/`_converged_commit` resolve `tip` from it.
    (Generalises Stage-5's _converged_producer_ledger; the confirmed-work git-sha
    witness re-derives because `tip` is a real 40-hex attested commit.)"""
    from peers.spine.op_config import OpConfig, load_op_config
    led = RunLedger(ledger_path)
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run=mode_run)
    led.append_attested(repo, tip, event="confirmed-work", subject="F1", status="pass",
                        witness={"kind": "git-sha", "uri": tip, "sha256": tip},
                        independence=True, mode_run=mode_run)
    led.append(event="stop", status="complete", mode_run=mode_run)
    return led


# ---- The dir-copy, NO-git fake provider (the orchestration unit-test seam) ----
class FakeWorktreeProvider:
    """Implements the WorktreeProvider Protocol WITHOUT git. lease() copies the
    repo dir to a tmp leaf; propagate() records the edge + witness only. Tracks
    every lease/release so a test can assert teardown ran (no leaked workspace)."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.leased: list[str] = []
        self.released: list[str] = []
        self.propagations: list[dict] = []

    @contextmanager
    def lease(self, repo, mode_run, *, base=None):
        # base_dir is OUTSIDE the repo (placement decision), so the copy never
        # nests. Exclude `.peers/worktrees` from the copy: if the source repo
        # ever held a real per-run worktree subtree there, a recursive copytree
        # would recurse into it (unbounded nesting) -- the explicit-allow-list
        # posture the real GitWorktreeProvider gets for free (it copies only the
        # _MIRROR names, never the whole repo). Also drop the parent's run.lock /
        # run.jsonl so the leased .peers/ starts clean (mirrors the real seed).
        wt = self.base_dir / f"wt-{mode_run}"
        shutil.copytree(repo, wt, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("worktrees"))
        (wt / ".peers").mkdir(exist_ok=True)
        for stale in ("run.lock", "run.jsonl"):
            (wt / ".peers" / stale).unlink(missing_ok=True)
        ws = RunWorkspace(worktree_path=wt, branch=f"peers/run/{mode_run}",
                          base_sha=base or "0" * 40, mode_run=mode_run)
        self.leased.append(mode_run)
        try:
            yield ws
        finally:
            self.released.append(mode_run)
            shutil.rmtree(wt, ignore_errors=True)

    def propagate(self, from_ws, to_ws, artifact):
        edge = {"from_run": from_ws.mode_run, "to_run": to_ws.mode_run,
                "artifact": artifact}
        self.propagations.append(edge)
        # the fake returns a file-style witness (no git) so the seam is testable
        return {"kind": "file", "uri": artifact, "sha256": "fake"}


def _frozen(*verdicts):
    """A tiny CONVERGED oracle stub: each call returns the next verdict (sticky
    on the last). Used to inject a deterministic CONVERGED decision into the
    propagate seam unit tests without building a full ledger."""
    box = {"i": 0, "v": list(verdicts) or [True]}

    def _check(rows, *, mode_run, repo):
        v = box["v"][min(box["i"], len(box["v"]) - 1)]
        box["i"] += 1
        return v

    return _check
