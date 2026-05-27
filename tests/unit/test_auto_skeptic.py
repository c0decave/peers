"""Tests for the post-convergence auto-skeptic hook.

When `consecutive_clean_ticks >= N` (convergence-reached gate would
fire), the orchestrator does NOT immediately exit with reason
"complete". Instead it runs ONE EXTRA tick whose prompt is prefixed
with a critical-re-audit header. If that tick stays clean, the next
loop iteration declares terminal success. If it surfaces a new
blocking bug, the counter resets to 0 and the loop continues normally.
"""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path

from peers.driver_orchestrator import (
    OrchestratorDriver,
    _AUTO_SKEPTIC_PROMPT_PREFIX,
)
from peers.peer_spec import PeerSpec
from peers.state_store import DEFAULT_STATE


def _init_repo(repo: Path) -> Path:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _make_driver(tmp_path: Path, **kw) -> OrchestratorDriver:
    repo = _init_repo(tmp_path / "repo")
    peer_dir = repo / ".peers"
    peer_dir.mkdir(mode=0o700)
    return OrchestratorDriver(
        repo=repo, peer_dir=peer_dir, goals=[],
        peer_specs=[
            PeerSpec(name=n, tool=n if n in ("claude", "codex") else "claude",
                     argv=("true",), prompt_mode="stdin")
            for n in ("claude", "codex")
        ],
        **kw,
    )


def _convergence_state() -> dict:
    s = copy.deepcopy(DEFAULT_STATE)
    s["iteration"] = 10
    s["consecutive_clean_ticks"] = 3
    return s


def test_default_auto_skeptic_is_enabled(tmp_path: Path) -> None:
    drv = _make_driver(tmp_path)
    assert drv.auto_skeptic_enabled is True


def test_opt_out_auto_skeptic(tmp_path: Path) -> None:
    drv = _make_driver(tmp_path, auto_skeptic_enabled=False)
    assert drv.auto_skeptic_enabled is False


def test_pre_tick_exit_at_convergence_sets_skeptic_pending(
    tmp_path: Path, monkeypatch,
) -> None:
    """When convergence fires the first time, _pre_tick_exit should
    return (None, …) and set _auto_skeptic_prompt_pending instead of
    returning complete."""
    drv = _make_driver(tmp_path)
    # Stub all_green_including_soft to True
    monkeypatch.setattr(drv, "_all_green_including_soft", lambda s: True)
    monkeypatch.setattr(drv, "_goal_mutation_reason", lambda: None)
    monkeypatch.setattr(drv.engine, "evaluate_hard_gates", lambda: {})
    monkeypatch.setattr(drv, "_record_results", lambda s, r: None)
    monkeypatch.setattr(drv, "_save_state", lambda s: None)

    state = _convergence_state()
    early_exit, _results = drv._pre_tick_exit(state, max_ticks=None, ticks=0)

    assert early_exit is None
    assert state.get("_auto_skeptic_prompt_pending") is True


def test_pre_tick_exit_completes_after_skeptic_ran(
    tmp_path: Path, monkeypatch,
) -> None:
    """If _auto_skeptic_ran_at indicates the skeptic just ran (= within
    the same iteration), pre_tick_exit returns complete."""
    drv = _make_driver(tmp_path)
    monkeypatch.setattr(drv, "_all_green_including_soft", lambda s: True)
    monkeypatch.setattr(drv, "_goal_mutation_reason", lambda: None)
    monkeypatch.setattr(drv.engine, "evaluate_hard_gates", lambda: {})
    monkeypatch.setattr(drv, "_record_results", lambda s, r: None)
    monkeypatch.setattr(drv, "_save_state", lambda s: None)
    monkeypatch.setattr(drv, "_append_exit_event", lambda r, t: None)

    state = _convergence_state()
    # Simulate: previous tick was the skeptic
    state["_auto_skeptic_ran_at"] = state["iteration"]  # 10

    early_exit, _ = drv._pre_tick_exit(state, max_ticks=None, ticks=0)

    assert early_exit is not None
    assert early_exit["reason"] == "complete"


def test_disabled_skeptic_skips_to_complete(
    tmp_path: Path, monkeypatch,
) -> None:
    drv = _make_driver(tmp_path, auto_skeptic_enabled=False)
    monkeypatch.setattr(drv, "_all_green_including_soft", lambda s: True)
    monkeypatch.setattr(drv, "_goal_mutation_reason", lambda: None)
    monkeypatch.setattr(drv.engine, "evaluate_hard_gates", lambda: {})
    monkeypatch.setattr(drv, "_record_results", lambda s, r: None)
    monkeypatch.setattr(drv, "_save_state", lambda s: None)
    monkeypatch.setattr(drv, "_append_exit_event", lambda r, t: None)

    state = _convergence_state()

    early_exit, _ = drv._pre_tick_exit(state, max_ticks=None, ticks=0)

    assert early_exit is not None
    assert early_exit["reason"] == "complete"
    # No skeptic flag set
    assert "_auto_skeptic_prompt_pending" not in state


def test_prepare_tick_prompt_injects_skeptic_prefix(
    tmp_path: Path, monkeypatch,
) -> None:
    drv = _make_driver(tmp_path)
    # Build the minimum state needed for _prepare_tick_prompt
    state = copy.deepcopy(DEFAULT_STATE)
    state["iteration"] = 5
    state["_auto_skeptic_prompt_pending"] = True

    from peers.turn_manager import TurnManager
    tm = TurnManager(state)

    # Stub the heavy inner functions
    monkeypatch.setattr(drv, "_read_inbox", lambda others, s, receiver: [])
    monkeypatch.setattr(drv, "_soft_reviews_pending", lambda s, p: [])

    peer, spec, prompt = drv._prepare_tick_prompt(state, tm, results={})

    # Skeptic prefix in prompt
    assert "POST-CONVERGENCE SKEPTIC RE-AUDIT" in prompt
    # Pending flag cleared
    assert "_auto_skeptic_prompt_pending" not in state
    # Ran-at iteration tracked = iteration+1 (the tick about to fire)
    assert state["_auto_skeptic_ran_at"] == 6


def test_prepare_tick_prompt_normal_when_no_skeptic_flag(
    tmp_path: Path, monkeypatch,
) -> None:
    drv = _make_driver(tmp_path)
    state = copy.deepcopy(DEFAULT_STATE)
    state["iteration"] = 5

    from peers.turn_manager import TurnManager
    tm = TurnManager(state)
    monkeypatch.setattr(drv, "_read_inbox", lambda others, s, receiver: [])
    monkeypatch.setattr(drv, "_soft_reviews_pending", lambda s, p: [])

    peer, spec, prompt = drv._prepare_tick_prompt(state, tm, results={})

    assert "POST-CONVERGENCE SKEPTIC RE-AUDIT" not in prompt
    assert "_auto_skeptic_ran_at" not in state


def test_skeptic_runs_again_after_counter_reset(
    tmp_path: Path, monkeypatch,
) -> None:
    """If a new bug resets consecutive_clean_ticks and convergence
    happens again later at a fresh iteration, skeptic should fire
    again — `jeweils` per user spec."""
    drv = _make_driver(tmp_path)
    monkeypatch.setattr(drv, "_all_green_including_soft", lambda s: True)
    monkeypatch.setattr(drv, "_goal_mutation_reason", lambda: None)
    monkeypatch.setattr(drv.engine, "evaluate_hard_gates", lambda: {})
    monkeypatch.setattr(drv, "_record_results", lambda s, r: None)
    monkeypatch.setattr(drv, "_save_state", lambda s: None)
    monkeypatch.setattr(drv, "_append_exit_event", lambda r, t: None)

    state = _convergence_state()
    # First convergence at iter=10 → skeptic ran at iter=11
    state["_auto_skeptic_ran_at"] = 11
    # Loop continued, counter reset, more clean ticks. Now at iter=20,
    # convergence fires again.
    state["iteration"] = 20

    early_exit, _ = drv._pre_tick_exit(state, max_ticks=None, ticks=0)

    # iter 20 vs last_skeptic 11 → 9 > 1 → fire skeptic again
    assert early_exit is None
    assert state.get("_auto_skeptic_prompt_pending") is True


def test_cli_without_post_convergence_skeptic_flag_propagates(
    monkeypatch,
) -> None:
    """`peers run --without-post-convergence-skeptic` passes the flag
    through cmd_run."""
    import peers.cli as cli

    captured: dict = {}

    def fake_cmd_run(target, max_ticks, dry_run=False, max_usd=None,
                     verbose=False, without_recon=False,
                     without_post_convergence_skeptic=False):
        captured["wpcs"] = without_post_convergence_skeptic
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_cmd_run)
    monkeypatch.setattr(
        "sys.argv", ["peers", "run", "--without-post-convergence-skeptic"],
    )

    rc = cli.main()

    assert rc == 0
    assert captured["wpcs"] is True


def test_cli_default_skeptic_enabled(monkeypatch) -> None:
    import peers.cli as cli

    captured: dict = {}

    def fake_cmd_run(target, max_ticks, dry_run=False, max_usd=None,
                     verbose=False, without_recon=False,
                     without_post_convergence_skeptic=False):
        captured["wpcs"] = without_post_convergence_skeptic
        return 0

    monkeypatch.setattr(cli, "cmd_run", fake_cmd_run)
    monkeypatch.setattr("sys.argv", ["peers", "run"])

    rc = cli.main()

    assert rc == 0
    assert captured["wpcs"] is False


def test_skeptic_prompt_prefix_constant_has_required_content() -> None:
    """Spot-check the prompt content has the operator-visible markers."""
    assert "POST-CONVERGENCE SKEPTIC RE-AUDIT" in _AUTO_SKEPTIC_PROMPT_PREFIX
    assert "convergence-reached" in _AUTO_SKEPTIC_PROMPT_PREFIX
    assert "blocking" in _AUTO_SKEPTIC_PROMPT_PREFIX.lower()
    # Must close with an end marker so it's clearly delineated from the
    # tick's normal prompt body
    assert "END SKEPTIC HEADER" in _AUTO_SKEPTIC_PROMPT_PREFIX
