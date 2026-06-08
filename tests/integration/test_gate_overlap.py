"""Tier-1 Part B integration: the loop overlaps the expensive gate eval under
the next peer turn. Cheap gates stay fresh (sync each tick); expensive verdicts
come from the async frozen-SHA eval when ready, else a synchronous fallback.
With pipeline_gates off it's the legacy full-sync path.
"""
from __future__ import annotations

import copy
import subprocess
import time
from pathlib import Path

from peers.driver_orchestrator import OrchestratorDriver
from peers.peer_spec import PeerSpec
from peers.goals import Goal
from peers.goal_engine import GoalResult
from peers.state_store import DEFAULT_STATE


def _specs(*names):
    return [PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin") for n in names]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _repo_with_marker(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / ".peers").mkdir()
    (path / "marker.txt").write_text("m\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _gates():
    return [
        Goal(id="lint", type="hard",
             cmd="test -f marker.txt", pass_when="exit_code == 0"),
        Goal(id="tests", type="hard",
             cmd="test -f marker.txt", pass_when="exit_code == 0",
             expensive=True),
    ]


def _drv(repo, pipeline):
    return OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers", goals=_gates(),
        peer_specs=_specs("claude", "codex"), pipeline_gates=pipeline,
    )


def test_pipeline_off_is_legacy_full_sync(tmp_path: Path) -> None:
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=False)
    assert drv.async_runner is None
    res = drv._evaluate_gates_for_tick()
    assert res["lint"].state == "pass"
    assert res["tests"].state == "pass"


def test_pipelined_bootstrap_runs_expensive_sync(tmp_path: Path) -> None:
    # First tick: nothing submitted yet -> expensive computed synchronously.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    assert drv.async_runner is not None
    res = drv._evaluate_gates_for_tick()
    assert res["lint"].state == "pass"
    assert res["tests"].state == "pass"


def test_pipelined_uses_overlapped_expensive_not_live_tree(
    tmp_path: Path,
) -> None:
    # Submit the eval on the good SHA, then break the LIVE tree. The expensive
    # verdict must come from the overlapped (stale, good) SHA = pass, while the
    # cheap gate is evaluated fresh on the live (broken) tree = fail. This
    # proves expensive is overlapped/stale AND cheap is fresh.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    good = drv.comm.head_sha()
    drv._submit_gate_eval(good)
    (repo / "marker.txt").unlink()  # live tree now fails the gate
    res = None
    for _ in range(300):
        res = drv._evaluate_gates_for_tick()
        if res["tests"].state == "pass":
            break
        time.sleep(0.02)
    assert res is not None
    assert res["tests"].state == "pass"   # overlapped good SHA (stale)
    assert res["lint"].state == "fail"    # cheap evaluated fresh on live tree


def test_stale_sha_verdict_rejected_after_new_commit(tmp_path: Path) -> None:
    # poll_latest() returns the freshest-DONE eval, which can be an OLDER sha
    # than current HEAD once the peer commits again. Applying that stale verdict
    # would corrupt the stuck/two-phase counters (and could trip a false
    # stuck:tests-pass). Submit the eval on the good SHA, then COMMIT a
    # regression so HEAD moves. The expensive verdict must be recomputed on the
    # live (broken) tree — NOT served from the stale good-SHA result.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    good = drv.comm.head_sha()
    drv._submit_gate_eval(good)
    # let the (near-instant) good-SHA eval finish so poll_latest has it ready
    time.sleep(0.5)
    # commit a regression: HEAD now != the submitted/evaluated SHA
    (repo / "marker.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "regress")
    assert drv.comm.head_sha() != good
    res = drv._evaluate_gates_for_tick()
    # stale good verdict (pass) must be rejected; live tree is broken -> fail
    assert res["tests"].state == "fail"


def test_pipelined_verdict_used_when_sha_matches_head(tmp_path: Path) -> None:
    # Counterpart to the staleness guard: when the polled verdict's SHA equals
    # current HEAD, it IS trusted (the overlap actually saves work). HEAD stays
    # `good`; only the live worktree is dirtied, so the committed-SHA verdict is
    # the correct expensive judgement and must be used.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    good = drv.comm.head_sha()
    drv._submit_gate_eval(good)
    (repo / "marker.txt").unlink()  # dirty live tree, HEAD unchanged
    res = None
    for _ in range(300):
        res = drv._evaluate_gates_for_tick()
        if res["tests"].state == "pass":
            break
        time.sleep(0.02)
    assert res is not None
    assert res["tests"].state == "pass"   # SHA matches HEAD -> overlap trusted
    assert res["lint"].state == "fail"    # cheap still fresh on the live tree


def test_terminal_recheck_reevaluates_expensive_fresh(tmp_path: Path) -> None:
    # B4 backstop: a stale verdict claims the expensive gate passes, but the
    # current tree has regressed. The terminal recheck must re-run the
    # expensive gates fresh on the live tree (-> fail), so convergence is
    # never declared on stale data. Cheap verdicts are left as-is.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    state = copy.deepcopy(DEFAULT_STATE)
    stale = {"lint": GoalResult("lint", "pass", 0),
             "tests": GoalResult("tests", "pass", 0)}
    (repo / "marker.txt").unlink()  # live tree regressed
    fresh = drv._terminal_fresh_recheck(state, stale)
    assert fresh["tests"].state == "fail"   # expensive re-run fresh on live
    assert fresh["lint"].state == "pass"    # cheap untouched by the recheck


def test_terminal_recheck_noop_when_pipeline_off(tmp_path: Path) -> None:
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=False)
    state = copy.deepcopy(DEFAULT_STATE)
    stale = {"tests": GoalResult("tests", "pass", 0)}
    assert drv._terminal_fresh_recheck(state, stale) is stale


def test_converged_runs_fresh_recheck_before_terminal(
    tmp_path: Path, monkeypatch,
) -> None:
    # The convergence gate must re-run the fresh recheck when the (stale)
    # verdict looks all-green, and report the POST-recheck verdict. Here the
    # stale verdict is green but the fresh recheck flips it red.
    repo = _repo_with_marker(tmp_path / "r")
    drv = _drv(repo, pipeline=True)
    state = copy.deepcopy(DEFAULT_STATE)
    results = {"tests": GoalResult("tests", "pass", 0)}
    calls = {"n": 0}

    def fake_all_green(_s):  # 1st (gate) green, 2nd (post-recheck) red
        calls["n"] += 1
        return calls["n"] == 1

    recheck = {"ran": False}
    orig = drv._terminal_fresh_recheck

    def spy(s, r):
        recheck["ran"] = True
        return orig(s, r)

    monkeypatch.setattr(drv, "_all_green_including_soft", fake_all_green)
    monkeypatch.setattr(drv, "_terminal_fresh_recheck", spy)
    assert drv._converged_after_fresh_recheck(state, results) is False
    assert recheck["ran"] is True
