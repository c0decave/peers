"""Progress-aware stuck reset (implement-mode tests-pass false-halt fix).

Diagnostic finding (calc brownfield/greenfield v2, 2026-05-31): the
convergence-wall `stuck_halt_after` watches `tests-pass`, but in
implement-mode the acceptance suite is red BY DESIGN until ~all PLAN
steps are built. A multi-step build that completes one step every few
ticks was therefore killed with `stuck:tests-pass` while genuinely
progressing.

Fix: when the count of completed PLAN steps increases on a tick, the
watched terminal gate's red streak is forgiven (its `stuck_counter` is
cleared). A run only halts when it makes NO step progress for N ticks
while the gate stays red — true no-progress, not feature-incompleteness.

HEAD advancing is NOT used as the progress signal: a handoff commit
advances HEAD every tick even when stuck, so it would disable the halt
entirely. PLAN-step completion is the only meaningful signal.
"""
from __future__ import annotations

from pathlib import Path


# ---- count_done_plan_steps ------------------------------------------------

def test_count_done_plan_steps_counts_checked_boxes(tmp_path: Path) -> None:
    from peers.driver_tick_hooks import count_done_plan_steps
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Plan\n\n"
        "- [x] [STEP-1] done lower\n"
        "- [X] [STEP-2] done upper\n"
        "- [ ] [STEP-3] still open\n"
        "  - [x] nested done\n"
        "some prose - [x] not at line start should not count as a step\n"
    )
    # 3 lines start with `- [x]`/`- [X]` (incl. the indented nested one);
    # the inline mid-sentence one does not.
    assert count_done_plan_steps(plan) == 3


def test_count_done_plan_steps_missing_file_is_zero(tmp_path: Path) -> None:
    from peers.driver_tick_hooks import count_done_plan_steps
    assert count_done_plan_steps(tmp_path / "nope.md") == 0


# ---- reset_stuck_on_progress ----------------------------------------------

def _state(counter: dict, **extra) -> dict:
    s = {"stuck_counter": dict(counter)}
    s.update(extra)
    return s


def test_first_observation_sets_baseline_without_reset() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"tests-pass": 4})
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    # No prior baseline → cannot conclude progress → counter untouched.
    assert state["stuck_counter"]["tests-pass"] == 4
    assert state["last_plan_steps_done"] == 2


def test_step_increase_clears_watched_counter() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"tests-pass": 4}, last_plan_steps_done=1)
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "tests-pass" not in state["stuck_counter"]
    assert state["last_plan_steps_done"] == 2


def test_malformed_config_containers_use_default_reset_gates_sad_path() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress

    for state in (
        _state(
            {"tests-pass": 4},
            last_plan_steps_done=1,
            config=["not-a-mapping"],
        ),
        _state(
            {"tests-pass": 4},
            last_plan_steps_done=1,
            config={"goals": ["not-a-mapping"]},
        ),
    ):
        reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
        assert "tests-pass" not in state["stuck_counter"]
        assert state["last_plan_steps_done"] == 2


def test_no_step_change_keeps_counter() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"tests-pass": 4}, last_plan_steps_done=2)
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert state["stuck_counter"]["tests-pass"] == 4
    assert state["last_plan_steps_done"] == 2


def test_only_resets_configured_gates_not_arbitrary_others() -> None:
    """Step progress forgives the configured reset gates only.

    Both watched-halt gates (tests-pass + no-prior-regression) are in the
    implement reset set (BUG-STUCK-01), but a gate that is NOT in the reset
    set — e.g. acceptance-pass, red by design and not watched for a
    convergence-wall halt — must keep its streak.
    """
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state(
        {"tests-pass": 4, "no-prior-regression": 4, "acceptance-pass": 4},
        last_plan_steps_done=1,
    )
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "tests-pass" not in state["stuck_counter"]
    assert "no-prior-regression" not in state["stuck_counter"]
    assert state["stuck_counter"]["acceptance-pass"] == 4


def test_non_implement_mode_is_noop() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"tests-pass": 4})
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="audit")
    # audit-mode: tests-pass red IS a real stuck signal — do not forgive,
    # and do not pollute state with the implement-only baseline key.
    assert state["stuck_counter"]["tests-pass"] == 4
    assert "last_plan_steps_done" not in state


def test_config_override_gate_list() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state(
        {"acceptance-pass": 4},
        last_plan_steps_done=1,
        config={"goals": {"stuck_progress_reset_gates": ["acceptance-pass"]}},
    )
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "acceptance-pass" not in state["stuck_counter"]


def test_config_empty_list_disables_even_in_implement() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state(
        {"tests-pass": 4},
        config={"goals": {"stuck_progress_reset_gates": []}},
    )
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert state["stuck_counter"]["tests-pass"] == 4
    # explicit empty override = fully disabled → no baseline tracking either
    assert "last_plan_steps_done" not in state


def test_step_count_drop_rebaselines_so_later_rise_resets() -> None:
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"tests-pass": 2}, last_plan_steps_done=4)
    # PLAN momentarily shows fewer done (e.g. transient edit): no reset,
    # but baseline drops so re-completing counts as progress next time.
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert state["stuck_counter"]["tests-pass"] == 2
    assert state["last_plan_steps_done"] == 2


def test_progress_averts_halt_end_to_end() -> None:
    """A run one tick from the halt that completes a step survives."""
    from peers.driver_tick_hooks import (
        compute_stuck_gate_halt_reason,
        reset_stuck_on_progress,
    )
    # tests-pass red for 5 ticks → would halt now.
    state = _state({"tests-pass": 5}, last_plan_steps_done=2)
    assert compute_stuck_gate_halt_reason(state) == "stuck:tests-pass"
    # but a PLAN step was just completed this tick:
    reset_stuck_on_progress(state, plan_steps_done=3, mode_name="implement")
    assert compute_stuck_gate_halt_reason(state) is None


# ---- BUG-STUCK-01: no-prior-regression progress reset ---------------------
# The convergence-wall halt watches BOTH tests-pass AND no-prior-regression
# (_DEFAULT_STUCK_HALT_GATES), but the implement progress-reset originally
# forgave only tests-pass — so a progressing band was false-halted
# `stuck:no-prior-regression` on band-external env drift (a re-snapshot-
# fragile baseline: -n worker count, mid-run toolchain installs, flaky
# async-under-xdist). The reset set must match the watched-halt set.

def test_step_increase_clears_no_prior_regression_in_implement() -> None:
    """Happy path: a progressing implement band forgives the env-fragile
    no-prior-regression streak just as it forgives tests-pass."""
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"no-prior-regression": 4}, last_plan_steps_done=1)
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "no-prior-regression" not in state["stuck_counter"]
    assert state["last_plan_steps_done"] == 2


def test_step_increase_clears_both_watched_gates_in_implement() -> None:
    """Happy path: both watched-halt gates are forgiven together on step
    progress — the reset set is symmetric with the halt-watch set."""
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state(
        {"tests-pass": 4, "no-prior-regression": 4}, last_plan_steps_done=1,
    )
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "tests-pass" not in state["stuck_counter"]
    assert "no-prior-regression" not in state["stuck_counter"]


def test_no_prior_regression_not_forgiven_in_audit_mode() -> None:
    """Sad path: outside implement-mode a red no-prior-regression IS a real
    stuck signal — progress must NOT forgive it, and the implement-only
    baseline key must not leak into the state."""
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"no-prior-regression": 4})
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="audit")
    assert state["stuck_counter"]["no-prior-regression"] == 4
    assert "last_plan_steps_done" not in state


def test_no_step_change_keeps_no_prior_regression() -> None:
    """Sad path: no PLAN progress this tick → the no-prior-regression streak
    stands, so a genuinely stalled band still halts."""
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state({"no-prior-regression": 4}, last_plan_steps_done=2)
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert state["stuck_counter"]["no-prior-regression"] == 4


def test_no_prior_regression_progress_averts_halt_end_to_end() -> None:
    """Edge: the R4 scenario — a band one tick from
    stuck:no-prior-regression that completes a PLAN step survives instead of
    being false-halted on band-external env drift."""
    from peers.driver_tick_hooks import (
        compute_stuck_gate_halt_reason,
        reset_stuck_on_progress,
    )
    state = _state({"no-prior-regression": 5}, last_plan_steps_done=2)
    assert compute_stuck_gate_halt_reason(state) == "stuck:no-prior-regression"
    reset_stuck_on_progress(state, plan_steps_done=3, mode_name="implement")
    assert compute_stuck_gate_halt_reason(state) is None


def test_config_override_can_exclude_no_prior_regression() -> None:
    """Edge: an operator can still opt out — a reset-gate list of only
    tests-pass keeps no-prior-regression watched-but-unforgiven even in
    implement-mode (the new default is overridable)."""
    from peers.driver_tick_hooks import reset_stuck_on_progress
    state = _state(
        {"tests-pass": 4, "no-prior-regression": 4},
        last_plan_steps_done=1,
        config={"goals": {"stuck_progress_reset_gates": ["tests-pass"]}},
    )
    reset_stuck_on_progress(state, plan_steps_done=2, mode_name="implement")
    assert "tests-pass" not in state["stuck_counter"]
    assert state["stuck_counter"]["no-prior-regression"] == 4
