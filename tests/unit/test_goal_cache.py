"""Tier-1 idle reduction: content-addressed memoization of code-pure hard gates.

A gate marked ``cacheable: true`` is expected to be a pure function of the
checked-out code. When both the tree and HEAD are unchanged, the (often
expensive — e.g. a full pytest run) subprocess can be skipped and the prior
PASS reused. The key includes the git tree hash of the ENTIRE working tree
(tracked + untracked + uncommitted) plus HEAD, so both content edits and
history-only commits invalidate the cache; any uncertainty (not a git repo,
git error) falls back to always running. By construction this can never return
a stale/wrong verdict.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.goal_engine import GoalEngine
from peers.goals import Goal


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _counter_goal(counter: Path, cacheable: bool) -> Goal:
    # The counter lives OUTSIDE the repo, so running the cmd does not change
    # the tree (which would otherwise invalidate the cache itself).
    return Goal(
        id="g", type="hard",
        cmd=f"bash -c 'echo x >> {counter}; exit 0'",
        pass_when="exit_code == 0",
        cacheable=cacheable,
    )


def _runs(counter: Path) -> int:
    return counter.read_text().count("x") if counter.exists() else 0


def test_cacheable_gate_skips_rerun_when_tree_unchanged(tmp_path: Path):
    repo = _make_repo(tmp_path)
    counter = tmp_path / "c"
    eng = GoalEngine([_counter_goal(counter, True)], repo)
    r1 = eng.evaluate_hard_gates()
    assert r1["g"].state == "pass"
    r2 = eng.evaluate_hard_gates()  # tree unchanged → must reuse cached verdict
    assert _runs(counter) == 1, "cacheable gate re-ran despite an unchanged tree"
    assert r2["g"].state == "pass"


def test_cacheable_gate_reruns_after_tracked_change(tmp_path: Path):
    repo = _make_repo(tmp_path)
    counter = tmp_path / "c"
    eng = GoalEngine([_counter_goal(counter, True)], repo)
    eng.evaluate_hard_gates()
    (repo / "a.txt").write_text("2\n")  # uncommitted tracked change
    eng.evaluate_hard_gates()
    assert _runs(counter) == 2, "cacheable gate did not re-run after a tree change"


def test_cacheable_gate_reruns_after_untracked_file(tmp_path: Path):
    # An untracked new file (e.g. a new test) can change a gate's verdict, so
    # the tree key must capture it (git add -A into a throwaway index).
    repo = _make_repo(tmp_path)
    counter = tmp_path / "c"
    eng = GoalEngine([_counter_goal(counter, True)], repo)
    eng.evaluate_hard_gates()
    (repo / "new_test.py").write_text("x = 1\n")  # untracked
    eng.evaluate_hard_gates()
    assert _runs(counter) == 2, "cacheable gate did not re-run after an untracked file appeared"


def test_noncacheable_gate_always_runs(tmp_path: Path):
    repo = _make_repo(tmp_path)
    counter = tmp_path / "c"
    eng = GoalEngine([_counter_goal(counter, False)], repo)
    eng.evaluate_hard_gates()
    eng.evaluate_hard_gates()
    assert _runs(counter) == 2, "a non-cacheable gate must run every time"


def test_cacheable_fail_is_never_cached(tmp_path: Path):
    # A FAIL must re-run every time (flaky-red can clear; real-red re-surfaces)
    # — only PASS verdicts are memoized.
    repo = _make_repo(tmp_path)
    counter = tmp_path / "c"
    failing = Goal(
        id="g", type="hard",
        cmd=f"bash -c 'echo x >> {counter}; exit 1'",
        pass_when="exit_code == 0",
        cacheable=True,
    )
    eng = GoalEngine([failing], repo)
    r1 = eng.evaluate_hard_gates()
    assert r1["g"].state == "fail"
    eng.evaluate_hard_gates()  # same tree, but a fail must NOT be cached
    assert _runs(counter) == 2, "a failing cacheable gate was wrongly cached"


def test_cacheable_failsafe_when_not_a_git_repo(tmp_path: Path):
    # No tree key computable → must NOT cache (fail-safe: always run).
    nonrepo = tmp_path / "nonrepo"
    nonrepo.mkdir()
    counter = tmp_path / "c"
    eng = GoalEngine([_counter_goal(counter, True)], nonrepo)
    eng.evaluate_hard_gates()
    eng.evaluate_hard_gates()
    assert _runs(counter) == 2, "cached despite no computable tree key (unsafe)"
