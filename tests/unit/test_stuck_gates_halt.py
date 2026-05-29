"""Item 7: tests-pass + no-prior-regression as convergence wall.

v9, v10, v11, v12 all stranded on tests-pass + no-prior-regression
timing out. The substrate kept burning budget while these gates stayed
red. A 'stuck_halt' threshold gives up + exits cleanly when a critical
hard gate has been red for N consecutive ticks.

Existing `stuck_counter` dict already tracks per-goal red streaks. This
test exercises a new helper that translates stuck_counter into a halt
reason when configured gates exceed threshold.
"""
from __future__ import annotations


def test_stuck_gate_below_threshold_returns_none() -> None:
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"tests-pass": 3, "no-prior-regression": 0},
    }
    assert compute_stuck_gate_halt_reason(state) is None


def test_stuck_gate_at_threshold_returns_reason() -> None:
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"tests-pass": 5},
    }
    reason = compute_stuck_gate_halt_reason(state)
    assert reason == "stuck:tests-pass"


def test_stuck_gate_above_threshold_returns_reason() -> None:
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"no-prior-regression": 7},
    }
    reason = compute_stuck_gate_halt_reason(state)
    assert reason == "stuck:no-prior-regression"


def test_multiple_stuck_gates_returns_highest_count() -> None:
    """When several gates stuck, name the worst one in the reason."""
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"tests-pass": 6, "no-prior-regression": 8},
    }
    reason = compute_stuck_gate_halt_reason(state)
    assert reason == "stuck:no-prior-regression"


def test_non_watched_gate_does_not_trigger_halt() -> None:
    """A non-critical hard gate stuck at 99 still doesn't halt the run."""
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"lint-clean": 99},  # not in critical set
    }
    assert compute_stuck_gate_halt_reason(state) is None


def test_threshold_overridable_via_config(monkeypatch) -> None:
    """Operators can raise / lower the threshold via .peers/config.yaml."""
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"tests-pass": 3},
        "config": {"goals": {"stuck_halt_after": 3}},
    }
    reason = compute_stuck_gate_halt_reason(state)
    assert reason == "stuck:tests-pass"


def test_extra_watched_gates_via_config() -> None:
    """Operators can add gates to the watched set."""
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"my-custom-gate": 10},
        "config": {"goals": {"stuck_halt_gates": ["my-custom-gate"]}},
    }
    reason = compute_stuck_gate_halt_reason(state)
    assert reason == "stuck:my-custom-gate"


def test_zero_threshold_disables_halt() -> None:
    """stuck_halt_after: 0 means never halt on stuck gates (legacy behavior)."""
    from peers.driver_tick_hooks import compute_stuck_gate_halt_reason
    state = {
        "stuck_counter": {"tests-pass": 999},
        "config": {"goals": {"stuck_halt_after": 0}},
    }
    assert compute_stuck_gate_halt_reason(state) is None
