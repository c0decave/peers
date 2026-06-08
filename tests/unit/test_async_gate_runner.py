"""Tier-1 Part B: AsyncGateRunner evaluates the expensive hard gates against
a frozen git SHA in a throwaway worktree, so the eval can overlap the next
peer's turn. Verdicts must match a synchronous eval on the same SHA; the
gitignored ``.peers/`` artifacts the gates read (cwd-relative) must be
mirrored into the worktree; git/worktree failures must be fail-safe.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from peers.goals import Goal
from peers.goal_engine import GoalEngine
from peers.async_gate_runner import AsyncGateRunner, GATE_EVAL_FAILED


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "marker.txt").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_worktree_eval_matches_sync(tmp_path: Path) -> None:
    repo, sha = _init_repo(tmp_path)
    g = Goal(id="marker", type="hard",
             cmd="test -f marker.txt", pass_when="exit_code == 0")
    sync = GoalEngine([g], cwd=repo).evaluate_hard_gates({"marker"})["marker"]
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[g], expensive_ids={"marker"})
    runner.submit(sha)
    got = runner.take(sha)
    assert sync.state == "pass"
    assert got["marker"].state == "pass"


def test_baseline_mirrored_into_worktree(tmp_path: Path) -> None:
    # A gate reading an UNTRACKED .peers/ artifact passes only if the runner
    # mirrors .peers/ into the frozen worktree (git worktree add only checks
    # out tracked files; .peers/ is gitignored runtime state).
    repo, sha = _init_repo(tmp_path)
    peers_dir = repo / ".peers"
    peers_dir.mkdir()
    (peers_dir / "passing-baseline.txt").write_text("t1\nt2\n")
    g = Goal(id="baseline", type="hard",
             cmd="test -f .peers/passing-baseline.txt",
             pass_when="exit_code == 0")
    runner = AsyncGateRunner(repo=repo, peers_dir=peers_dir,
                             goals=[g], expensive_ids={"baseline"})
    runner.submit(sha)
    got = runner.take(sha)
    assert got["baseline"].state == "pass"


def test_eval_failure_is_fail_safe(tmp_path: Path) -> None:
    repo, _sha = _init_repo(tmp_path)
    g = Goal(id="x", type="hard", cmd="true", pass_when="exit_code == 0")
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[g], expensive_ids={"x"})
    bad = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    runner.submit(bad)
    assert runner.take(bad) is GATE_EVAL_FAILED


def test_take_unknown_sha_returns_none(tmp_path: Path) -> None:
    repo, _sha = _init_repo(tmp_path)
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[], expensive_ids=set())
    assert runner.take("never-submitted") is None


def _second_commit(repo: Path) -> str:
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")
    return _git(repo, "rev-parse", "HEAD")


def test_poll_latest_returns_freshest_done_and_discards_older(
    tmp_path: Path,
) -> None:
    repo, sha1 = _init_repo(tmp_path)
    sha2 = _second_commit(repo)
    g = Goal(id="m", type="hard",
             cmd="test -f marker.txt", pass_when="exit_code == 0")
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[g], expensive_ids={"m"})
    runner.submit(sha1)
    runner.submit(sha2)
    # Poll (non-blocking) until the freshest SHA's eval is ready.
    got = None
    for _ in range(200):
        got = runner.poll_latest()
        if got is not None and got[0] == sha2:
            break
        time.sleep(0.02)
    assert got is not None and got[0] == sha2
    assert got[1]["m"].state == "pass"
    # sha1 was superseded by sha2 and discarded; nothing left to take.
    assert runner.poll_latest() is None


def test_resubmit_same_sha_does_not_leak_or_duplicate(tmp_path: Path) -> None:
    # No-new-commit ticks resubmit the same HEAD. Submitting the same sha twice
    # must NOT overwrite the in-flight Future (leaking an uncancelled pytest)
    # nor duplicate the order queue (which desyncs poll_latest/take).
    repo, sha = _init_repo(tmp_path)
    g = Goal(id="m", type="hard",
             cmd="test -f marker.txt", pass_when="exit_code == 0")
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[g], expensive_ids={"m"})
    runner.submit(sha)
    first = runner._futures[sha]
    runner.submit(sha)
    assert runner._order.count(sha) == 1     # not duplicated in the queue
    assert runner._futures[sha] is first     # first Future preserved, not leaked
    got = runner.take(sha)
    assert got["m"].state == "pass"


def test_poll_latest_none_when_nothing_submitted(tmp_path: Path) -> None:
    repo, _sha = _init_repo(tmp_path)
    runner = AsyncGateRunner(repo=repo, peers_dir=repo / ".peers",
                             goals=[], expensive_ids=set())
    assert runner.poll_latest() is None


def test_prune_stale_gate_worktrees(tmp_path: Path) -> None:
    from peers.async_gate_runner import prune_stale_gate_worktrees
    repo, sha = _init_repo(tmp_path)
    wt = tmp_path / "peers-gate-leftover"
    _git(repo, "worktree", "add", "--detach", str(wt), sha)
    listing = _git(repo, "worktree", "list")
    assert "peers-gate-leftover" in listing
    assert prune_stale_gate_worktrees(repo) >= 1
    assert "peers-gate-leftover" not in _git(repo, "worktree", "list")
