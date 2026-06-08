"""Flake-tolerance: a hard gate may declare `retry_on_fail: N` so a transient
(load-induced) failure is absorbed before the gate reports red.

Root cause this fixes: a single flaky timing test under container load turned
`tests-pass` red, which tripped the substrate's `stuck:tests-pass` halt and
bricked a internal testing run at 7% of budget (v19, 2026-06-06). The pytest-based
gates now retry once, mirroring `no_regression`'s internal retry, so a flake
never makes the gate red and `stuck` is never triggered spuriously. A genuine
failure still fails after the retries.
"""
from __future__ import annotations

from pathlib import Path

from peers.goal_engine import GoalEngine
from peers.goals import Goal


def _flaky_goal(counter: Path, retries: int, fail_until: int) -> Goal:
    # The cmd fails its first `fail_until` invocations, then passes — using an
    # external counter so we can assert how many attempts were made.
    cmd = (
        f"bash -c 'n=$(cat {counter} 2>/dev/null || echo 0); "
        f"echo $((n+1)) > {counter}; [ $n -ge {fail_until} ] && exit 0 || exit 1'"
    )
    return Goal(id="g", type="hard", cmd=cmd, pass_when="exit_code == 0",
                retry_on_fail=retries)


def _attempts(counter: Path) -> int:
    return int(counter.read_text()) if counter.exists() else 0


def test_retry_absorbs_a_transient_flake(tmp_path: Path):
    counter = tmp_path / "c"
    eng = GoalEngine([_flaky_goal(counter, retries=1, fail_until=1)], tmp_path)
    r = eng.evaluate_hard_gates()
    assert r["g"].state == "pass"   # failed once, retried, passed
    assert _attempts(counter) == 2


def test_retry_still_fails_on_a_real_failure(tmp_path: Path):
    counter = tmp_path / "c"
    eng = GoalEngine([_flaky_goal(counter, retries=1, fail_until=99)], tmp_path)
    r = eng.evaluate_hard_gates()
    assert r["g"].state == "fail"   # always fails → still fails after retry
    assert _attempts(counter) == 2  # 1 attempt + 1 retry


def test_no_retry_by_default(tmp_path: Path):
    counter = tmp_path / "c"
    eng = GoalEngine([_flaky_goal(counter, retries=0, fail_until=1)], tmp_path)
    r = eng.evaluate_hard_gates()
    assert r["g"].state == "fail"   # default: first failure stands
    assert _attempts(counter) == 1
