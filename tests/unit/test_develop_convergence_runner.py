"""R4: a real convergence runner — the ``inner`` the develop ContractImplementer
(via ``worktree_convergence``) was always missing. It drives an injected agent
to implement a frozen contract inside a worktree, runs the contract's acceptance
each attempt, and only on a REAL pass + a REAL diff makes a REAL git commit.

Honesty seam (load-bearing): the runner returns ``(True, sha, branch)`` ONLY
when acceptance actually passed AND a non-empty diff was committed. It can never
manufacture convergence: no passing acceptance -> no commit -> ``(False, ...)``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.develop.convergence import AgentConvergenceRunner


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("def f():\n    return 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# --- happy path ---------------------------------------------------------------
def test_happy_converges_first_try_makes_real_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    def agent(_prompt: str, workdir: Path) -> str:
        (Path(workdir) / "fix.txt").write_text("fixed", encoding="utf-8")
        return "done"

    def acceptance(workdir: Path) -> tuple[bool, str]:
        return ((Path(workdir) / "fix.txt").exists(), "ok")

    runner = AgentConvergenceRunner(run_agent=agent, run_acceptance=acceptance)
    ok, sha, branch = runner(repo)
    assert ok is True
    assert sha and len(sha) == 40 and sha != base   # a real, new commit
    assert _git(repo, "rev-parse", "HEAD") == sha
    assert branch  # the real branch name


def test_happy_converges_on_later_attempt_within_budget(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    calls = {"n": 0}

    def agent(_prompt: str, workdir: Path) -> str:
        calls["n"] += 1
        if calls["n"] >= 2:        # only the 2nd attempt actually fixes it
            (Path(workdir) / "fix.txt").write_text("fixed", encoding="utf-8")
        return "iterating"

    def acceptance(workdir: Path) -> tuple[bool, str]:
        return ((Path(workdir) / "fix.txt").exists(), "pending")

    ok, sha, _ = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=acceptance, budget=5)(repo)
    assert ok is True and sha
    assert calls["n"] == 2


# --- sad path -----------------------------------------------------------------
def test_sad_never_passing_acceptance_makes_no_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    def agent(_prompt: str, workdir: Path) -> str:
        (Path(workdir) / "junk.txt").write_text("noise", encoding="utf-8")
        return "tried"

    def acceptance(_workdir: Path) -> tuple[bool, str]:
        return (False, "still failing")

    ok, sha, branch = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=acceptance, budget=3)(repo)
    assert ok is False
    assert sha is None
    # the honesty seam: HEAD is untouched — no convergence claim without a pass.
    assert _git(repo, "rev-parse", "HEAD") == base


def test_sad_acceptance_passes_but_no_diff_is_not_convergence(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")

    def agent(_prompt: str, _workdir: Path) -> str:
        return "I made no changes"      # touches nothing

    def acceptance(_workdir: Path) -> tuple[bool, str]:
        return (True, "vacuously green")  # green but no real work

    ok, sha, _ = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=acceptance, budget=2)(repo)
    assert ok is False and sha is None
    assert _git(repo, "rev-parse", "HEAD") == base


# --- edge cases ---------------------------------------------------------------
def test_edge_budget_one_failing_calls_agent_once(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    calls = {"n": 0}

    def agent(_p: str, _w: Path) -> str:
        calls["n"] += 1
        return "x"

    ok, _, _ = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=lambda w: (False, ""), budget=1)(repo)
    assert ok is False
    assert calls["n"] == 1


def test_edge_attest_peer_marks_converged_commit_with_substrate_note(tmp_path: Path) -> None:
    # confirmed-work needs an attested commit; when an attest_peer is given the
    # runner records the peers-attest note on its convergence commit so the
    # develop gate resolves a real (not None) author — a legit attest, not a forge.
    from peers.attest import attested_peer

    repo = _init_repo(tmp_path)

    def agent(_p: str, workdir: Path) -> str:
        (Path(workdir) / "fix.txt").write_text("fixed", encoding="utf-8")
        return "done"

    ok, sha, _ = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=lambda w: ((Path(w) / "fix.txt").exists(), ""),
        attest_peer="develop")(repo)
    assert ok is True and sha
    assert attested_peer(repo, sha) == "develop"


def test_sad_vacuous_green_with_attest_peer_writes_no_note(tmp_path: Path) -> None:
    # TQ-02: a non-convergence (here vacuous green) must mint NO peers-attest
    # note — attestation only ever attaches to a real converged commit.
    from peers.attest import attested_peer

    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    ok, sha, _ = AgentConvergenceRunner(
        run_agent=lambda p, w: "noop",            # no real change
        run_acceptance=lambda w: (True, ""),      # vacuously green
        attest_peer="develop")(repo)
    assert ok is False and sha is None
    assert attested_peer(repo, base) is None      # no note minted on the base


def test_edge_agent_exception_in_an_attempt_does_not_abort_the_loop(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    calls = {"n": 0}

    def agent(_p: str, workdir: Path) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient model error")
        (Path(workdir) / "fix.txt").write_text("fixed", encoding="utf-8")
        return "recovered"

    ok, sha, _ = AgentConvergenceRunner(
        run_agent=agent, run_acceptance=lambda w: ((Path(w) / "fix.txt").exists(), ""),
        budget=5)(repo)
    assert ok is True and sha
    assert calls["n"] == 2
