import copy
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from peers.driver_orchestrator import (
    OrchestratorDriver, BudgetCheck, _apply_config_budget,
)
from peers.health_guard import RunResult
from peers.goal_engine import GoalResult
from peers.peer_spec import PeerSpec
from peers.state_store import DEFAULT_STATE

ROOT_FOR_TESTS = Path(__file__).parent.parent.parent


def _specs(*names: str) -> list[PeerSpec]:
    return [PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin") for n in names]


def _budget_state(**overrides):
    base = {
        "budget": {
            "max_iterations": 10, "max_runtime_s": 100,
            "spent_iterations": 0, "spent_runtime_s": 0,
            "max_consecutive_failures": 5,
            "consecutive_failures": 0,
            "max_usd": 5.0, "spent_usd": 0.0,
            "max_usd_mode": "hard",
        },
        "warnings": [],
    }
    base["budget"].update(overrides)
    return base


def test_budget_max_usd_hard_mode_returns_reason():
    state = _budget_state(max_usd=5.0, spent_usd=6.0, max_usd_mode="hard")
    assert BudgetCheck(state).reason() == "max_usd"


def test_budget_max_usd_warn_mode_does_not_return_reason():
    state = _budget_state(max_usd=5.0, spent_usd=6.0, max_usd_mode="warn")
    assert BudgetCheck(state).reason() is None


def test_budget_max_usd_warn_mode_emits_warning_once():
    state = _budget_state(max_usd=5.0, spent_usd=6.0, max_usd_mode="warn")
    BudgetCheck(state).reason()
    BudgetCheck(state).reason()
    BudgetCheck(state).reason()
    # idempotent — only one warning, not three:
    relevant = [w for w in state["warnings"] if "max_usd:" in w]
    assert len(relevant) == 1
    assert "mode=warn" in relevant[0]


def test_budget_max_usd_warn_mode_stays_one_time_after_prompt_pop():
    state = _budget_state(max_usd=5.0, spent_usd=6.0, max_usd_mode="warn")
    BudgetCheck(state).reason()
    state["warnings"].clear()  # mirrors prompt consumption in the driver loop
    BudgetCheck(state).reason()
    assert state["warnings"] == []


def test_budget_max_usd_off_mode_silent_no_warning():
    state = _budget_state(max_usd=5.0, spent_usd=6.0, max_usd_mode="off")
    assert BudgetCheck(state).reason() is None
    assert state["warnings"] == []


def test_budget_max_usd_legacy_no_mode_defaults_hard():
    """Pre-Phase-3i state files have no `max_usd_mode` key. We must
    still treat them as hard so existing API-key users aren't surprised
    by a silently-disabled cap."""
    state = _budget_state()
    del state["budget"]["max_usd_mode"]
    state["budget"]["spent_usd"] = 6.0
    assert BudgetCheck(state).reason() == "max_usd"


def test_apply_config_budget_auto_with_oauth_picks_warn(
    tmp_path, monkeypatch,
):
    """End-to-end: config says `max_usd_mode: auto`, peers are OAuth,
    `_apply_config_budget` must resolve to `warn` and persist it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Synth-home with OAuth markers.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    monkeypatch.setattr(Path, "home", lambda: home)

    state = copy.deepcopy(DEFAULT_STATE)
    cfg_budget = {"max_usd": 5.0, "max_usd_mode": "auto"}
    _apply_config_budget(state, cfg_budget, peer_tools=["claude", "codex"])
    assert state["budget"]["max_usd_mode"] == "warn"
    assert "OAuth" in state["budget"].get("max_usd_mode_reason", "")


def test_apply_config_budget_omitted_mode_re_resolves_stale_state(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = copy.deepcopy(DEFAULT_STATE)
    state["budget"]["max_usd_mode"] = "warn"

    _apply_config_budget(state, {"max_usd": 5.0}, peer_tools=["codex"])

    assert state["budget"]["max_usd_mode"] == "hard"
    assert "API key" in state["budget"].get("max_usd_mode_reason", "")


def test_budget_iterations_exceeded():
    state = {"budget": {"max_iterations": 10, "max_runtime_s": 100,
                        "spent_iterations": 10, "spent_runtime_s": 0,
                        "max_consecutive_failures": 5,
                        "consecutive_failures": 0}}
    assert BudgetCheck(state).reason() == "max_iterations"


def test_budget_runtime_exceeded():
    state = {"budget": {"max_iterations": 10, "max_runtime_s": 100,
                        "spent_iterations": 1, "spent_runtime_s": 101,
                        "max_consecutive_failures": 5,
                        "consecutive_failures": 0}}
    assert BudgetCheck(state).reason() == "max_runtime"


def test_budget_consecutive_failures_exceeded():
    state = {"budget": {"max_iterations": 10, "max_runtime_s": 100,
                        "spent_iterations": 1, "spent_runtime_s": 1,
                        "max_consecutive_failures": 3,
                        "consecutive_failures": 4}}
    assert BudgetCheck(state).reason() == "max_consecutive_failures"


def test_budget_consecutive_failures_equality_exits():
    state = {"budget": {"max_iterations": 10, "max_runtime_s": 100,
                        "spent_iterations": 1, "spent_runtime_s": 1,
                        "max_consecutive_failures": 3,
                        "consecutive_failures": 3}}
    assert BudgetCheck(state).reason() == "max_consecutive_failures"


def test_budget_ok():
    state = {"budget": {"max_iterations": 10, "max_runtime_s": 100,
                        "spent_iterations": 1, "spent_runtime_s": 1,
                        "max_consecutive_failures": 3,
                        "consecutive_failures": 0}}
    assert BudgetCheck(state).reason() is None


def test_apply_config_budget_overrides_limits_keeps_spent():
    state = copy.deepcopy(DEFAULT_STATE)
    state["budget"]["spent_iterations"] = 7
    state["budget"]["spent_runtime_s"] = 42
    state["budget"]["consecutive_failures"] = 1
    cfg_budget = {
        "max_iterations": 999,
        "max_runtime_s": 7200,
        "max_consecutive_failures": 8,
    }
    _apply_config_budget(state, cfg_budget)
    assert state["budget"]["max_iterations"] == 999
    assert state["budget"]["max_runtime_s"] == 7200
    assert state["budget"]["max_consecutive_failures"] == 8
    # spent_* untouched:
    assert state["budget"]["spent_iterations"] == 7
    assert state["budget"]["spent_runtime_s"] == 42
    assert state["budget"]["consecutive_failures"] == 1


def test_apply_config_budget_ignores_unknown_keys():
    state = copy.deepcopy(DEFAULT_STATE)
    cfg_budget = {"max_iterations": 5, "bogus_field": 99}
    _apply_config_budget(state, cfg_budget)
    assert state["budget"]["max_iterations"] == 5
    assert "bogus_field" not in state["budget"]


# --- Fix 8: success requires a proper handoff commit ---------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "x").write_text("x")
    _git(path, "add", "x")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _make_driver(repo: Path, peer_names=("claude", "codex")) -> OrchestratorDriver:
    return OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers",
        goals=[], peer_specs=_specs(*peer_names),
    )


def _ok_run() -> RunResult:
    return RunResult(classification="success", exit_code=0,
                     stdout="", stderr="", duration_ms=10)


def _state_for(peer: str) -> dict:
    s = copy.deepcopy(DEFAULT_STATE)
    s["turn_index"] = s["peer_order"].index(peer)
    return s


def test_post_run_no_commit_is_soft_fail(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    drv._head_before_invoke = drv.comm.head_sha()
    s = _state_for("claude")
    assert drv._post_run(s, "claude", _ok_run()) is False
    assert s["peers"]["claude"]["last_run"]["soft_fail_reason"]


def test_post_run_commit_without_handoff_is_soft_fail(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    drv._head_before_invoke = drv.comm.head_sha()
    (repo / "y").write_text("y")
    _git(repo, "add", "y")
    _git(repo, "commit", "-q", "-m",
         "Half-done\n\nbody\n\nPeer: claude\n")
    s = _state_for("claude")
    assert drv._post_run(s, "claude", _ok_run()) is False


def test_post_run_proper_handoff_is_success(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    drv._head_before_invoke = drv.comm.head_sha()
    (repo / "y").write_text("y")
    _git(repo, "add", "y")
    _git(repo, "commit", "-q", "-m",
         "Done\n\n## Self-Review\nok\n\n"
         "Self-Review: pass\nPeer-Status: handoff\nPeer: claude\n")
    s = _state_for("claude")
    assert drv._post_run(s, "claude", _ok_run()) is True


def test_peer_output_log_refuses_symlinked_log_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    repo = _make_repo(tmp_path / "r")
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (peer_dir / "log").symlink_to(outside, target_is_directory=True)
    drv = _make_driver(repo)
    drv._peer_dir_identity = drv._capture_peer_dir_identity()

    drv._write_peer_output_logs(
        1,
        "claude",
        RunResult(
            classification="success",
            exit_code=0,
            stdout="captured peer output\n",
            stderr="",
            duration_ms=1,
        ),
    )

    err = capsys.readouterr().err
    assert "could not write per-tick peer log" in err
    assert "refusing symlinked dir" in err
    assert not (outside / "peers" / "tick-00001-claude.stdout.log").exists()


def test_prompt_log_refuses_symlinked_log_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    repo = _make_repo(tmp_path / "r")
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (peer_dir / "log").symlink_to(outside, target_is_directory=True)
    drv = _make_driver(repo)
    drv._peer_dir_identity = drv._capture_peer_dir_identity()

    drv._write_prompt_log(1, "claude", "captured prompt\n")

    err = capsys.readouterr().err
    assert "could not write per-tick prompt log" in err
    assert "refusing symlinked dir" in err
    assert not (outside / "prompts" / "tick-00001-claude.txt").exists()


def test_post_run_classification_not_success_is_soft_fail(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    drv._head_before_invoke = drv.comm.head_sha()
    s = _state_for("claude")
    bad = RunResult(classification="process-fail", exit_code=1,
                    stdout="", stderr="x", duration_ms=1)
    assert drv._post_run(s, "claude", bad) is False


# --- Fix 18: per-tick runs.jsonl ----------------------------------------


def test_budget_exit_re_evaluates_goals(tmp_path: Path):
    """Regression for the Phase-3h live-run finding: when the loop
    exits via the BudgetCheck path (e.g. max_usd), it must run a final
    `evaluate_hard_gates()` before saving state — otherwise the
    persisted `goals_status` reflects the START-of-tick snapshot, not
    the post-tick reality, and `peers status` / `peers report` lie.

    Scenario: zero-cost peer commits a file that flips a hard goal
    from fail→pass, and we pre-load the budget so the second tick
    is blocked by max_usd. After the run, state.json's goals_status
    must show `pass`, matching what `peers verify` would compute.
    """
    import json as _json
    from peers.goals import Goal

    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    (peer_dir / "log").mkdir()
    fake = [sys.executable, str(ROOT_FOR_TESTS / "tests"
                                / "fixtures" / "fake_peer.py")]
    # Hard goal that flips to pass as soon as the file exists.
    flipper = Goal(
        id="needs-marker", type="hard",
        cmd=f"test -f {target / 'MARKER'} && echo ok",
        pass_when="exit_code == 0",
    )
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir,
        goals=[flipper],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=tuple(fake), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=tuple(fake), prompt_mode="stdin"),
        ],
        idle_timeout_s=10, absolute_max_runtime_s=20,
        cfg_budget={"max_usd": 0.01, "max_usd_mode": "hard"},
    )

    # Inject "spent_usd > max_usd" before the loop's second iteration
    # by patching the tick handler to create MARKER and bump spent_usd
    # after the first tick. We do this by running max_ticks=1 first,
    # mutating state, and re-entering with run() — but the simpler
    # approach is to set spent_usd in DEFAULT_STATE before run().
    state_path = peer_dir / "state.json"
    # Pre-seed state so BudgetCheck trips at top of tick 1 ... but that
    # would exit without running any tick. We want a tick to complete
    # first. So instead: pre-create MARKER, run max_ticks=1, then
    # manually call _loop semantics via inflating spent_usd to >max_usd
    # via state_store and re-running.
    (target / "MARKER").write_text("ok")
    _git(target, "add", "MARKER")
    _git(target, "commit", "-q", "-m", "marker\n\nPeer: setup\n")

    # Pre-seed: spent_usd already over the cap so the loop exits at top.
    from peers.state_store import DEFAULT_STATE
    seed = copy.deepcopy(DEFAULT_STATE)
    seed["budget"]["max_usd"] = 0.01
    seed["budget"]["spent_usd"] = 0.05
    seed["budget"]["max_usd_mode"] = "hard"
    seed["budget"]["max_iterations"] = 10
    seed["budget"]["max_runtime_s"] = 100
    seed["budget"]["max_consecutive_failures"] = 5
    # Critical: pre-populate goals_status with STALE "fail" entries so
    # we can prove the re-eval at exit overwrites them.
    seed["goals_status"] = {
        "needs-marker": {"state": "fail", "diagnostic": "stale"},
    }
    state_path.write_text(_json.dumps(seed))

    result = drv.run(max_ticks=None)
    assert result["reason"] == "budget:max_usd"

    saved = _json.loads(state_path.read_text())
    # The re-eval at exit must have flipped this to pass.
    assert saved["goals_status"]["needs-marker"]["state"] == "pass", (
        f"budget-exit failed to re-evaluate hard goals; "
        f"saved={saved['goals_status']}"
    )


def test_post_tick_goal_refresh_clears_stale_failure(tmp_path: Path, monkeypatch):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    state = copy.deepcopy(DEFAULT_STATE)
    state["goals_status"] = {
        "tests-pass": {"state": "fail", "diagnostic": "stale"},
    }
    state["stuck_counter"] = {"tests-pass": 2}

    monkeypatch.setattr(
        drv.engine,
        "evaluate_hard_gates",
        lambda **_kwargs: {
            "tests-pass": GoalResult("tests-pass", "pass", 5),
        },
    )

    drv._refresh_goals_after_tick(state, ["tests-pass"])

    assert state["goals_status"]["tests-pass"]["state"] == "pass"
    assert "tests-pass" not in state["stuck_counter"]


def test_post_tick_goal_refresh_does_not_double_count_persistent_failure(
    tmp_path: Path, monkeypatch,
):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    state = copy.deepcopy(DEFAULT_STATE)
    state["goals_status"] = {
        "tests-pass": {"state": "fail", "diagnostic": "pre-tick"},
    }
    state["stuck_counter"] = {"tests-pass": 4}

    monkeypatch.setattr(
        drv.engine,
        "evaluate_hard_gates",
        lambda **_kwargs: {
            "tests-pass": GoalResult(
                "tests-pass", "fail", 5, diagnostic="still red",
            ),
        },
    )

    drv._refresh_goals_after_tick(state, ["tests-pass"])

    assert state["goals_status"]["tests-pass"]["state"] == "fail"
    assert state["goals_status"]["tests-pass"]["diagnostic"] == "still red"
    assert state["stuck_counter"]["tests-pass"] == 4


def test_post_tick_goal_refresh_records_new_failure(
    tmp_path: Path, monkeypatch,
):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    state = copy.deepcopy(DEFAULT_STATE)
    state["goals_status"] = {
        "tests-pass": {"state": "pass", "diagnostic": ""},
    }
    state["stuck_counter"] = {}

    monkeypatch.setattr(
        drv.engine,
        "evaluate_hard_gates",
        lambda **_kwargs: {
            "tests-pass": GoalResult(
                "tests-pass", "fail", 5, diagnostic="new breakage",
            ),
        },
    )

    drv._refresh_goals_after_tick(state, ["tests-pass"])

    assert state["goals_status"]["tests-pass"]["state"] == "fail"
    assert state["goals_status"]["tests-pass"]["diagnostic"] == "new breakage"
    assert state["stuck_counter"]["tests-pass"] == 1


def test_runs_jsonl_is_appended_per_tick(tmp_path: Path):
    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    (peer_dir / "log").mkdir()
    fake = [sys.executable, str(ROOT_FOR_TESTS / "tests"
                                / "fixtures" / "fake_peer.py")]
    # A never-passing hard goal so the loop actually runs ticks
    # instead of short-circuiting via all_green (which is now True
    # for zero-hard-goal configs).
    from peers.goals import Goal
    never_pass = Goal(
        id="never", type="hard",
        cmd="false", pass_when="exit_code == 0",
    )
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir,
        goals=[never_pass],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=tuple(fake), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=tuple(fake), prompt_mode="stdin"),
        ],
        idle_timeout_s=10, absolute_max_runtime_s=20,
    )
    drv.run(max_ticks=2)
    log_path = peer_dir / "log" / "runs.jsonl"
    assert log_path.exists(), "runs.jsonl must be written by the driver"
    all_lines = [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line
    ]
    # Filter out the synthetic "exit" event line added at run termination.
    tick_lines = [e for e in all_lines if e.get("event") != "exit"]
    exit_lines = [e for e in all_lines if e.get("event") == "exit"]
    assert len(tick_lines) == 2
    assert len(exit_lines) == 1
    assert exit_lines[0]["reason"] == "max_ticks"
    for entry in tick_lines:
        for key in ("ts", "iteration", "peer", "tool", "classification",
                    "duration_ms", "success"):
            assert key in entry, f"missing key {key} in {entry}"


def test_run_refuses_symlinked_run_lock_before_truncating(tmp_path: Path):
    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    bait = tmp_path / "bait.lock"
    bait.write_text("keep me")
    (peer_dir / "run.lock").symlink_to(bait)
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir, goals=[],
        peer_specs=_specs("claude", "codex"),
    )

    with pytest.raises(RuntimeError, match="symlink"):
        drv.run(max_ticks=0)

    assert bait.read_text() == "keep me"


def test_run_lock_held_closes_peer_dir_identity_fd(tmp_path: Path):
    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    lock_fp = (peer_dir / "run.lock").open("a")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        drv = OrchestratorDriver(
            repo=target, peer_dir=peer_dir, goals=[],
            peer_specs=_specs("claude", "codex"),
            recon_enabled=False, codemap_enabled=False,
        )

        result = drv.run(max_ticks=0)

        assert result["reason"] == "lock-held"
        assert drv._peer_dir_identity_fd is None
    finally:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()


def test_run_max_ticks_closes_peer_dir_identity_fd(tmp_path: Path):
    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir, goals=[],
        peer_specs=_specs("claude", "codex"),
        recon_enabled=False, codemap_enabled=False,
    )

    result = drv.run(max_ticks=0)

    assert result["reason"] == "max_ticks"
    assert drv._peer_dir_identity_fd is None


def test_append_run_log_refuses_late_symlink_swap(tmp_path: Path):
    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    (peer_dir / "log").mkdir(parents=True)
    bait = tmp_path / "bait.log"
    bait.write_text("keep me")
    (peer_dir / "log" / "runs.jsonl").symlink_to(bait)
    drv = OrchestratorDriver(
        repo=target, peer_dir=peer_dir, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    state = copy.deepcopy(DEFAULT_STATE)

    with pytest.raises(OSError):
        drv._append_run_log(state, "claude", _ok_run(), success=True)

    assert bait.read_text() == "keep me"


def test_run_refuses_peer_dir_swap_after_peer_turn(tmp_path: Path):
    import sys
    from peers.goals import Goal

    target = _make_repo(tmp_path / "t")
    peer_dir = target / ".peers"
    peer_dir.mkdir()
    (peer_dir / "goals.yaml").write_text("goals: []\n")
    outside = tmp_path / "outside-peers"
    outside.mkdir()
    script = tmp_path / "swap_peers.py"
    script.write_text(
        "import os, pathlib, shutil\n"
        "p = pathlib.Path('.peers')\n"
        "shutil.move(str(p), str(pathlib.Path('old-peers')))\n"
        "p.symlink_to(os.environ['PEERS_OUTSIDE'], target_is_directory=True)\n"
    )
    drv = OrchestratorDriver(
        repo=target,
        peer_dir=peer_dir,
        goals=[Goal(id="never", type="hard", cmd="false",
                    pass_when="exit_code == 0")],
        peer_specs=[
            PeerSpec(name="claude", tool="claude",
                     argv=(sys.executable, str(script)), prompt_mode="stdin"),
            PeerSpec(name="codex", tool="codex",
                     argv=("true",), prompt_mode="stdin"),
        ],
    )

    old_env = os.environ.get("PEERS_OUTSIDE")
    os.environ["PEERS_OUTSIDE"] = str(outside)
    try:
        with pytest.raises(RuntimeError, match="refusing control-plane IO|symlink"):
            drv.run(max_ticks=1)
    finally:
        if old_env is None:
            os.environ.pop("PEERS_OUTSIDE", None)
        else:
            os.environ["PEERS_OUTSIDE"] = old_env

    assert not (outside / "state.json").exists()


# --- Fix 9: inbox cursor advances to last consumed commit ----------------

def test_update_peer_health_marks_degraded_after_three_fails(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    for _ in range(3):
        drv._update_peer_health(s, "claude", success=False)
    assert s["peers"]["claude"]["state"] == "degraded"


def test_update_peer_health_recovers_on_success(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    for _ in range(3):
        drv._update_peer_health(s, "claude", success=False)
    assert s["peers"]["claude"]["state"] == "degraded"
    drv._update_peer_health(s, "claude", success=True)
    assert s["peers"]["claude"]["state"] == "healthy"


def test_phase1_degraded_annotations_recorded(tmp_path: Path, capsys):
    """(post-2026-05-24): when a peer is promoted to degraded
    we persist (a) `degraded_reason`, (b) `degraded_at_iter`, AND
    emit a stderr marker the operator can grep. Without this v4 left
    `peers-ctl status` showing only `state=degraded` with no hint why
    or when."""
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    s["iteration"] = 7
    s["peers"]["claude"]["last_run"] = {
        "classification": "api-error",
        "matched_error_pattern": (
            r"(?im)^[^\"]*?\b(ERROR|FATAL)\b[^\"]*?\bauthentication"
        ),
        "matched_error_snippet": (
            "2026-05-24T... ERROR auth: authentication failed for "
            "OAuth token"
        ),
    }
    for _ in range(3):
        drv._update_peer_health(s, "claude", success=False)
    t = s["peers"]["claude"]
    assert t["state"] == "degraded"
    assert t.get("degraded_at_iter") == 7
    reason = t.get("degraded_reason", "")
    assert "recent-fails:3/5" in reason
    assert "api-error" in reason
    captured = capsys.readouterr()
    assert "marked DEGRADED" in captured.err
    assert "claude" in captured.err


def test_phase1_degraded_annotations_cleared_on_recovery(tmp_path: Path):
    """Recovery (success after degraded) must drop the annotations so
    a later operator dump doesn't suggest the peer is still struggling."""
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    s["iteration"] = 5
    s["peers"]["claude"]["last_run"] = {"classification": "api-error"}
    for _ in range(3):
        drv._update_peer_health(s, "claude", success=False)
    assert "degraded_reason" in s["peers"]["claude"]
    drv._update_peer_health(s, "claude", success=True)
    t = s["peers"]["claude"]
    assert t["state"] == "healthy"
    assert "degraded_reason" not in t
    assert "degraded_at_iter" not in t


def test_phase1_degraded_annotations_only_set_on_transition(tmp_path: Path):
    """Edge: while the peer STAYS degraded across more fails, the
    `degraded_at_iter` must NOT bump forward — we want the operator
    to see the iter where degradation first happened, not the most
    recent tick."""
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    s["iteration"] = 3
    s["peers"]["claude"]["last_run"] = {"classification": "api-error"}
    for _ in range(3):
        drv._update_peer_health(s, "claude", success=False)
    iter_at_first = s["peers"]["claude"]["degraded_at_iter"]
    assert iter_at_first == 3
    # Now keep failing — iteration advanced.
    s["iteration"] = 9
    drv._update_peer_health(s, "claude", success=False)
    assert s["peers"]["claude"]["degraded_at_iter"] == iter_at_first


def test_halted_md_written_when_all_degraded(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    peer_dir = tmp_path / "p"
    peer_dir.mkdir()
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[],
        peer_specs=_specs("claude", "codex"),
    )
    s = copy.deepcopy(DEFAULT_STATE)
    s["peers"]["claude"]["state"] = "degraded"
    s["peers"]["codex"]["state"] = "degraded"
    halt = drv._maybe_halt(s)
    halted = peer_dir / "HALTED.md"
    assert halt == {"reason": "peer-unavailable:all-peers-degraded", "state": s}
    assert halted.exists()
    assert "all peers degraded" in halted.read_text()


def test_halted_md_n3_requires_all_three_degraded(tmp_path: Path):
    """With n=3 the loop must NOT halt while one peer is still healthy."""
    repo = _make_repo(tmp_path / "r")
    peer_dir = tmp_path / "p"
    peer_dir.mkdir()
    drv = OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[],
        peer_specs=_specs("claude", "codex", "claude-2"),
    )
    s = copy.deepcopy(DEFAULT_STATE)
    s["peer_order"] = ["claude", "codex", "claude-2"]
    s["peers"]["claude"] = {"state": "degraded", "consecutive_fails": 0,
                            "recent_fails": 0, "recent_runs": []}
    s["peers"]["codex"] = {"state": "degraded", "consecutive_fails": 0,
                           "recent_fails": 0, "recent_runs": []}
    s["peers"]["claude-2"] = {"state": "healthy", "consecutive_fails": 0,
                              "recent_fails": 0, "recent_runs": []}
    assert drv._maybe_halt(s) is None
    assert not (peer_dir / "HALTED.md").exists()
    # Mark the third as degraded → now we halt.
    s["peers"]["claude-2"]["state"] = "degraded"
    halt = drv._maybe_halt(s)
    assert halt == {"reason": "peer-unavailable:all-peers-degraded", "state": s}
    assert (peer_dir / "HALTED.md").exists()


def test_inbox_cursor_advances_to_last_consumed_commit(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    s = copy.deepcopy(DEFAULT_STATE)
    # First call seeds last_inbox_sha to current HEAD
    drv._read_inbox(["codex"], s)
    head_after_seed = s["last_inbox_sha"]["codex"]
    assert head_after_seed == drv.comm.head_sha()
    # codex makes 2 commits
    for i in range(2):
        (repo / f"c{i}").write_text("x")
        _git(repo, "add", f"c{i}")
        _git(repo, "commit", "-q", "-m",
             f"codex {i}\n\nPeer: codex\n")
    last_codex_sha = drv.comm.head_sha()
    # claude (the "current") makes one commit AFTER codex's
    (repo / "claude_work").write_text("x")
    _git(repo, "add", "claude_work")
    _git(repo, "commit", "-q", "-m",
         "claude work\n\nPeer: claude\n")
    # Now read inbox: should pick up the 2 codex commits, cursor moves
    # to last_codex_sha (NOT to current HEAD).
    msgs = drv._read_inbox(["codex"], s)
    assert len(msgs) == 2
    assert s["last_inbox_sha"]["codex"] == last_codex_sha


# --- _format_tick_status: operator-readable log labels ----------------

def test_format_tick_status_success_returns_handoff() -> None:
    """A successful tick (success=True) labels as `handoff` —
    the substrate-canonical word for 'peer made a valid commit with
    Peer-Status: handoff + Self-Review: pass trailers'."""
    from peers.driver_orchestrator import _format_tick_status
    assert _format_tick_status(success=True, classification="success") == "handoff"


def test_format_tick_status_no_commit_drops_confusing_fail_label() -> None:
    """Operator UX fix (2026-05-26): the old label `fail(success)` was
    actively confusing — `fail` and `success` next to each other look
    like a contradiction. New label is `no-handoff`, which says
    plainly: peer process ran clean, just didn't produce a handoff
    commit (no new commit, missing trailers, etc)."""
    from peers.driver_orchestrator import _format_tick_status
    assert _format_tick_status(success=False, classification="success") == "no-handoff"


def test_format_tick_status_real_failure_uses_classification_alone() -> None:
    """When the peer subprocess actually errored, the classification
    itself (process-fail / api-error / idle-timeout / absolute-timeout)
    is the clearest label — no need to wrap it in `fail(…)`."""
    from peers.driver_orchestrator import _format_tick_status
    assert _format_tick_status(
        success=False, classification="process-fail",
    ) == "process-fail"
    assert _format_tick_status(
        success=False, classification="api-error",
    ) == "api-error"
    assert _format_tick_status(
        success=False, classification="idle-timeout",
    ) == "idle-timeout"
    assert _format_tick_status(
        success=False, classification="absolute-timeout",
    ) == "absolute-timeout"
