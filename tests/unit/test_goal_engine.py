import shlex
import sys
import time
from pathlib import Path

from peers.goals import Goal
from peers.goal_engine import GoalEngine


def _hard_goal(id_: str, cmd: str, pass_when: str) -> Goal:
    return Goal(id=id_, type="hard", cmd=cmd, pass_when=pass_when)


def test_evaluates_hard_gate_pass(tmp_path: Path):
    g = _hard_goal("ok", "true", "exit_code == 0")
    engine = GoalEngine([g], cwd=tmp_path)
    results = engine.evaluate_hard_gates()
    assert results["ok"].state == "pass"


def test_evaluates_hard_gate_fail(tmp_path: Path):
    g = _hard_goal("bad", "false", "exit_code == 0")
    engine = GoalEngine([g], cwd=tmp_path)
    results = engine.evaluate_hard_gates()
    assert results["bad"].state == "fail"


def test_timeout_is_a_fail(tmp_path: Path):
    g = _hard_goal("slow", "sleep 5", "exit_code == 0")
    engine = GoalEngine([g], cwd=tmp_path, timeout_s=1)
    results = engine.evaluate_hard_gates()
    assert results["slow"].state == "fail"
    assert "timeout" in results["slow"].diagnostic.lower()


def test_timeout_does_not_wait_for_daemonized_child_holding_pipes(tmp_path: Path):
    child = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], "
        "stdout=sys.stdout, stderr=sys.stderr, start_new_session=True)"
    )
    g = _hard_goal(
        "daemon",
        f"{sys.executable} -c {shlex.quote(child)}",
        "exit_code == 0",
    )
    engine = GoalEngine([g], cwd=tmp_path, timeout_s=1)

    t0 = time.monotonic()
    results = engine.evaluate_hard_gates()
    elapsed = time.monotonic() - t0

    assert results["daemon"].state == "fail"
    assert "timeout" in results["daemon"].diagnostic.lower()
    assert elapsed < 3.5


def test_timeout_still_applies_after_process_closes_output_fds(tmp_path: Path):
    child = "import os, time; os.close(1); os.close(2); time.sleep(5)"
    g = _hard_goal(
        "closed-fds",
        f"exec {sys.executable} -c {shlex.quote(child)}",
        "exit_code == 0",
    )
    engine = GoalEngine([g], cwd=tmp_path, timeout_s=1)

    t0 = time.monotonic()
    results = engine.evaluate_hard_gates()
    elapsed = time.monotonic() - t0

    assert results["closed-fds"].state == "fail"
    assert "timeout" in results["closed-fds"].diagnostic.lower()
    assert elapsed < 3.5


def test_goal_output_capture_is_bounded(tmp_path: Path):
    from peers.goal_engine import _GOAL_OUTPUT_CAP_BYTES, _run_goal_cmd

    proc = _run_goal_cmd(
        "python3 -c \"import sys; sys.stdout.write('x' * 3000000)\"",
        tmp_path,
        10,
    )

    assert proc.returncode == 0
    assert "goal output truncated" in proc.stdout
    assert len(proc.stdout.encode()) < _GOAL_OUTPUT_CAP_BYTES + 200


def test_skips_soft_goals(tmp_path: Path):
    soft = Goal(id="x", type="soft", prompt="bla", reviewer="other")
    engine = GoalEngine([soft], cwd=tmp_path)
    assert engine.evaluate_hard_gates() == {}


def test_all_green_true_when_all_pass(tmp_path: Path):
    g = _hard_goal("ok", "true", "exit_code == 0")
    engine = GoalEngine([g], cwd=tmp_path)
    engine.evaluate_hard_gates()
    assert engine.all_green() is True


def test_all_green_false_when_any_fail(tmp_path: Path):
    g1 = _hard_goal("ok", "true", "exit_code == 0")
    g2 = _hard_goal("bad", "false", "exit_code == 0")
    engine = GoalEngine([g1, g2], cwd=tmp_path)
    engine.evaluate_hard_gates()
    assert engine.all_green() is False


def test_all_green_false_before_first_evaluation(tmp_path: Path):
    """With at least one hard goal declared, all_green starts False
    (we haven't evaluated yet) and only flips True after an
    evaluate_hard_gates() call that sees every goal pass."""
    g = _hard_goal("x", "true", "exit_code == 0")
    engine = GoalEngine([g], cwd=tmp_path)
    assert engine.all_green() is False


def test_run_goal_cmd_does_not_leak_fds_on_normal_exit(tmp_path: Path):
    """BUG-001 reproducer (2026-05-24): _run_goal_cmd() creates a
    selectors.DefaultSelector() and two pipe FDs per invocation, but
    only closes them on the SIGKILL/timeout path. On normal completion
    the selector's epoll fd + pipe fds leak. Substrate runs many goal
    evaluations per tick → eventually NPROC/NOFILE exhaustion.

    Counts /proc/self/fd before vs. after 50 successful goal runs;
    growth must be bounded by a small constant (interpreter caching,
    not per-call allocation). Skipped on platforms without /proc/self/fd
    (non-Linux)."""
    import os
    from peers.goal_engine import _run_goal_cmd

    proc_fd_dir = Path("/proc/self/fd")
    if not proc_fd_dir.is_dir():
        import pytest
        pytest.skip("requires /proc/self/fd (Linux)")

    # Warmup: first few calls may allocate caches; measure steady-state.
    for _ in range(3):
        _run_goal_cmd("true", tmp_path, 5)

    fds_before = len(os.listdir(proc_fd_dir))
    for _ in range(50):
        proc = _run_goal_cmd("true", tmp_path, 5)
        assert proc.returncode == 0
    fds_after = len(os.listdir(proc_fd_dir))

    leaked = fds_after - fds_before
    # Allow a tiny slack for legitimate interpreter-internal fd churn,
    # but reject the ~150-fd leak (3 fds/call × 50 calls) the bug
    # produces.
    assert leaked < 10, (
        f"fd leak detected: {leaked} fds accumulated over 50 calls "
        f"(before={fds_before}, after={fds_after}); each call should "
        "release its selector epoll fd + both pipe fds"
    )


def test_all_green_true_when_no_hard_goals(tmp_path: Path):
    """Zero-hard-goal configurations are trivially green at the hard
    level (the caller still has to confirm soft-goal consensus
    elsewhere via _all_green_including_soft)."""
    engine = GoalEngine([], cwd=tmp_path)
    assert engine.all_green() is True


def test_all_green_true_when_only_soft_goals(tmp_path: Path):
    """A config with only soft goals must report hard-green so the
    driver's _all_green_including_soft can move on to the soft check."""
    soft = Goal(id="docs", type="soft", prompt="check", reviewer="other")
    engine = GoalEngine([soft], cwd=tmp_path)
    assert engine.all_green() is True
