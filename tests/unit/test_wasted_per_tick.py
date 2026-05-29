"""Item 6: per-tick attribution of wasted_runtime_s.

Operators were left to math `tick N: 504s wasted, tick N+1: 1704s wasted`
from the running sum. Now `state['budget']['wasted_runtime_per_tick']`
holds the last 20 fail-tick entries with iteration + peer + duration_s.
"""
from __future__ import annotations

from peers.budget_accountant import record_tick_accounting


def _bare_state() -> dict:
    return {
        "iteration": 0,
        "budget": {
            "spent_iterations": 0,
            "spent_runtime_s": 0,
            "consecutive_failures": 0,
        },
    }


def test_fail_tick_records_per_tick_entry() -> None:
    state = _bare_state()
    record_tick_accounting(state, success=False, tick_dt=504, peer="claude")
    per_tick = state["budget"]["wasted_runtime_per_tick"]
    assert len(per_tick) == 1
    assert per_tick[0]["iteration"] == 1
    assert per_tick[0]["peer"] == "claude"
    assert per_tick[0]["duration_s"] == 504


def test_success_tick_does_not_record() -> None:
    state = _bare_state()
    record_tick_accounting(state, success=True, tick_dt=600, peer="claude")
    assert "wasted_runtime_per_tick" not in state["budget"]
    assert state["budget"].get("wasted_runtime_s", 0) == 0


def test_multiple_fail_ticks_accumulate_in_order() -> None:
    state = _bare_state()
    record_tick_accounting(state, success=False, tick_dt=504, peer="claude")
    record_tick_accounting(state, success=False, tick_dt=1704, peer="claude")
    record_tick_accounting(state, success=False, tick_dt=1800, peer="claude")
    per_tick = state["budget"]["wasted_runtime_per_tick"]
    assert [e["duration_s"] for e in per_tick] == [504, 1704, 1800]
    assert [e["iteration"] for e in per_tick] == [1, 2, 3]
    assert state["budget"]["wasted_runtime_s"] == 504 + 1704 + 1800


def test_per_tick_caps_at_20_entries() -> None:
    state = _bare_state()
    for _ in range(25):
        record_tick_accounting(state, success=False, tick_dt=100, peer="claude")
    per_tick = state["budget"]["wasted_runtime_per_tick"]
    assert len(per_tick) == 20
    # Oldest entries dropped: first remaining iter should be 6 (1..5 dropped).
    assert per_tick[0]["iteration"] == 6
    assert per_tick[-1]["iteration"] == 25


def test_peer_optional_none_records_cleanly() -> None:
    state = _bare_state()
    record_tick_accounting(state, success=False, tick_dt=300)  # peer omitted
    per_tick = state["budget"]["wasted_runtime_per_tick"]
    assert per_tick[0]["peer"] is None
    assert per_tick[0]["duration_s"] == 300
