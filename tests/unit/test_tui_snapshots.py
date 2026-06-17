"""Wave-1a: snapshot dataclasses are importable and have the fields windows render."""
from __future__ import annotations

from peers_ctl.tui import snapshots as S


def test_dataclasses_have_expected_fields():
    g = S.GateView(id="tests-pass", kind="hard", state="pass", stuck=0,
                   duration_ms=12, diagnostic="", cached=False, consensus=None)
    assert g.id == "tests-pass" and g.state == "pass"
    p = S.PeerView(name="claude", state="healthy", consecutive_fails=0.0,
                   recent_runs=[True, 0.5, False], last_run={})
    assert p.state in S.PEER_STATES
    assert S.PEER_STATES == ("healthy", "degraded", "halted", "unavailable")


def test_remaining_view_models_construct():
    # sad/edge: every view-model the readers emit must construct with its
    # documented defaults so a malformed source never crashes the renderer.
    b = S.BudgetView(spent_runtime_s=0, max_runtime_s=None, spent_tokens=0,
                     max_tokens=None, spent_usd=0.0, max_usd=None,
                     max_usd_mode=None, max_usd_mode_reason=None,
                     consecutive_failures=0, wasted_runtime=[])
    assert b.max_runtime_s is None and b.wasted_runtime == []

    t = S.TickEntry(iteration=None, peer=None, classification=None, success=None,
                    tokens=0, usd=0.0, head_before=None, head_after=None,
                    warnings=[], ts="t")
    assert t.is_exit is False and t.exit_reason is None  # defaults present

    c = S.ConvergenceView(consecutive_clean_ticks=0, convergence_phase=None,
                          phase_b_extra_ticks=None)
    assert c.convergence_phase is None

    r = S.RunSnapshot(name="run", state_present=False, iteration=0, mode=None,
                      phase=None, current_peer=None)
    assert r.gates == [] and r.peers == [] and r.budget is None
    assert r.convergence is None and r.warnings == []

    f = S.FleetEntry(name="p", path="/tmp/p", state="unknown", pid=None,
                     iteration=None, gates_green=None, gates_total=None,
                     alert=False)
    assert f.state == "unknown" and f.alert is False


def test_views_are_frozen():
    # edge: view-models are immutable so renderers can cache them safely.
    import dataclasses

    g = S.GateView(id="g", kind="hard", state="pass", stuck=0, duration_ms=0,
                   diagnostic="", cached=True, consensus=None)
    try:
        g.id = "mutated"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("GateView must be frozen")
