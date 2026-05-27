"""Integration tests with misbehaving peer fixtures.

Three scenarios from the round-2 code review:
1. wrong trailer (Self-Review: needs-review) — must soft-fail
2. trailing junk commit after a valid handoff — must succeed after H9
   (handoff trailers on ANY commit in the turn count)
3. uncommitted work in tree — must soft-fail with "no commit by peer"
   AND surface state["dirty_worktree"]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
FIX = ROOT / "tests" / "fixtures"


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _init_target(tmp_path: Path) -> Path:
    t = tmp_path / "target"
    t.mkdir()
    _git(t, "init", "-q", "-b", "main")
    _git(t, "config", "user.email", "x@y")
    _git(t, "config", "user.name", "x")
    (t / "README").write_text("z\n")
    (t / "widget.py").write_text("# seed\n")
    _git(t, "add", "README", "widget.py")
    _git(t, "commit", "-q", "-m", "init")
    return t


def _run_peers(cwd, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(cwd), *args],
        capture_output=True, text=True, env=env,
    )


def _write_config(target: Path, fixture: Path) -> None:
    cfg = target / ".peers" / "config.yaml"
    cfg.write_text(
        "driver: orchestrator\ncomm: git\n"
        "peers:\n"
        f"  - {{name: claude, tool: claude, argv: ['{sys.executable}', '{fixture}']}}\n"
        f"  - {{name: codex,  tool: codex,  argv: ['{sys.executable}', '{fixture}']}}\n"
        "budget: {max_iterations: 4, max_runtime_s: 60,"
        " max_consecutive_failures: 10}\n"
        "health: {idle_timeout_s: 30, absolute_max_runtime_s: 60}\n"
    )


def test_wrong_self_review_trailer_is_soft_fail(tmp_path: Path):
    target = _init_target(tmp_path)
    assert _run_peers(target, "init").returncode == 0
    _write_config(target, FIX / "fake_peer_wrong_trailer.py")

    r = _run_peers(target, "run", "--max-ticks", "2")
    assert r.returncode == 0, r.stderr
    state = json.loads((target / ".peers" / "state.json").read_text())
    last = state["peers"]["claude"]["last_run"]
    # Process exited 0, but trailer validation failed → success=False
    assert last["classification"] == "success"
    assert "Self-Review" in last.get("soft_fail_reason", "")
    assert state["budget"]["consecutive_failures"] >= 1


def test_trailing_junk_commit_still_counts_as_handoff(tmp_path: Path):
    """After Phase-2 fix H9, a valid handoff anywhere in the turn is
    accepted even if a subsequent commit lacks the trailers."""
    target = _init_target(tmp_path)
    assert _run_peers(target, "init").returncode == 0
    _write_config(target, FIX / "fake_peer_trailing_junk.py")

    r = _run_peers(target, "run", "--max-ticks", "1")
    assert r.returncode == 0, r.stderr
    state = json.loads((target / ".peers" / "state.json").read_text())
    # The valid handoff (the FIRST commit of the turn) made this a
    # successful tick despite the trailing junk commit.
    assert state["budget"]["consecutive_failures"] == 0
    last = state["peers"]["claude"]["last_run"]
    assert "soft_fail_reason" not in last


def test_uncommitted_work_is_soft_fail_and_flags_dirty(tmp_path: Path):
    target = _init_target(tmp_path)
    assert _run_peers(target, "init").returncode == 0
    _write_config(target, FIX / "fake_peer_uncommitted.py")

    r = _run_peers(target, "run", "--max-ticks", "1")
    assert r.returncode == 0, r.stderr
    state = json.loads((target / ".peers" / "state.json").read_text())
    last = state["peers"]["claude"]["last_run"]
    assert last["classification"] == "success"
    assert last.get("soft_fail_reason") == "no commit by peer this turn"
    assert state.get("dirty_worktree") is True
    # status command surfaces the dirty-tree warning
    s = _run_peers(target, "status")
    assert s.returncode == 0
    assert "uncommitted" in s.stdout.lower()
