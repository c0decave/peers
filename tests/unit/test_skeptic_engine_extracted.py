from __future__ import annotations

from peers.goal_engine import GoalResult
from peers.skeptic_engine import SkepticEngine, _resolve_convergence_state


def _result(goal_id: str, state: str = "pass") -> GoalResult:
    return GoalResult(goal_id=goal_id, state=state, duration_ms=1)


def test_resolve_convergence_state_is_exported_from_skeptic_engine():
    assert _resolve_convergence_state("implement", "A", 5, 5, 2, 0) == "B"
    assert _resolve_convergence_state("audit", "A", 5, 5, 2, 0) == "A"


def test_skeptic_engine_ignores_non_implement_modes():
    state: dict = {}
    results = {"tests-pass": _result("tests-pass")}

    SkepticEngine("audit").update_two_phase_counters(state, results)

    assert state == {}


def test_skeptic_engine_promotes_phase_a_after_hard_green_streak():
    state = {"consecutive_hard_green_ticks": 4, "convergence_phase": "A"}
    results = {"tests-pass": _result("tests-pass")}

    SkepticEngine("implement").update_two_phase_counters(state, results)

    assert state["consecutive_hard_green_ticks"] == 5
    assert state["convergence_phase"] == "B"
    assert state["phase_b_extra_ticks"] == 0


def test_skeptic_engine_counts_phase_b_only_when_skeptic_gates_green():
    state = {"convergence_phase": "B", "consecutive_hard_green_ticks": 5}
    results = {
        "tests-pass": _result("tests-pass"),
        "blind-review": _result("blind-review"),
        "honesty-audit": _result("honesty-audit"),
        "concerns-resolved": _result("concerns-resolved"),
    }

    SkepticEngine("implement").update_two_phase_counters(state, results)
    SkepticEngine("implement").update_two_phase_counters(state, results)

    assert state["phase_b_extra_ticks"] == 2
    assert state["convergence_phase"] == "complete"


def test_skeptic_engine_handles_empty_results_dict_edge():
    # edge: zero gates evaluated (e.g. first tick of a brand-new project
    # before any goal has been wired) must NOT crash and must NOT credit
    # a hard-green tick — `all(...) if results else False` is the guard.
    state: dict = {}
    SkepticEngine("implement").update_two_phase_counters(state, {})
    assert state["consecutive_hard_green_ticks"] == 0
    assert state["convergence_phase"] == "A"


def test_resolve_convergence_state_skips_unknown_phase_label_edge():
    # edge: a corrupt state.json may store an unknown phase label
    # ("X", ""). The resolver must fall back to "A" rather than
    # propagate the unknown phase or raise. (Greenfield/migration
    # boundary — pin the recovery semantics.)
    assert _resolve_convergence_state("implement", "X", 5, 5, 2, 0) == "A"
    assert _resolve_convergence_state("implement", "", 0, 5, 2, 0) == "A"


def test_skeptic_engine_resets_phase_b_when_gate_fails():
    state = {
        "convergence_phase": "B",
        "consecutive_hard_green_ticks": 5,
        "phase_b_extra_ticks": 1,
    }
    results = {
        "tests-pass": _result("tests-pass"),
        "blind-review": _result("blind-review"),
        "honesty-audit": _result("honesty-audit", "fail"),
        "concerns-resolved": _result("concerns-resolved"),
    }

    SkepticEngine("implement").update_two_phase_counters(state, results)

    assert state["phase_b_extra_ticks"] == 0
    assert state["convergence_phase"] == "B"
