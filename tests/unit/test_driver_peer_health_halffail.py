"""Bug C: productive-commit-no-handoff counts as half-fail in recent_fails.

Real fails (idle-timeout, no commit at all, history-rewrite) still 1.0.
Background: v12 tick 9+10 had claude produce real commits (review batch,
test additions) without `Peer-Status: handoff` / `Self-Review: pass`
trailers. Old logic counted those as full fails, pushing claude to
DEGRADED at recent_fails 3/5 even though claude was productively
hunting + writing fix attempts. Half-fail credit keeps the peer healthy
while still surfacing the missing-trailer issue.
"""
from __future__ import annotations

from peers.driver_peer_health import DriverPeerHealthMixin


def _bare_state(peer: str = "claude") -> dict:
    return {
        "peers": {peer: {"state": "healthy", "consecutive_fails": 0,
                          "recent_fails": 0, "recent_runs": []}},
        "iteration": 0,
    }


class _Driver(DriverPeerHealthMixin):
    """Mixin; instantiate via a tiny concrete subclass for tests."""
    def __init__(self) -> None:
        pass


def test_productive_no_handoff_counts_as_half_fail() -> None:
    drv = _Driver()
    state = _bare_state()
    # Simulate productive-no-handoff: _post_run set the flag.
    state["peers"]["claude"]["last_tick_productive_no_handoff"] = True
    drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["recent_fails"] == 0.5
    assert state["peers"]["claude"]["state"] == "healthy"


def test_real_fail_still_counts_as_full() -> None:
    drv = _Driver()
    state = _bare_state()
    state["peers"]["claude"]["last_tick_productive_no_handoff"] = False
    drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["recent_fails"] == 1.0
    assert state["peers"]["claude"]["state"] == "healthy"


def test_four_productive_no_handoffs_not_degraded() -> None:
    """4 productive-no-handoffs = 2.0 fail credit < 3 → still healthy.
    Old behavior would have hit DEGRADED at 3/5."""
    drv = _Driver()
    state = _bare_state()
    for _ in range(4):
        state["peers"]["claude"]["last_tick_productive_no_handoff"] = True
        drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["recent_fails"] == 2.0
    assert state["peers"]["claude"]["state"] == "healthy"


def test_three_real_fails_still_degraded() -> None:
    drv = _Driver()
    state = _bare_state()
    for _ in range(3):
        state["peers"]["claude"]["last_tick_productive_no_handoff"] = False
        drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["recent_fails"] == 3.0
    assert state["peers"]["claude"]["state"] == "degraded"


def test_mixed_real_and_partial_still_degrades_at_threshold() -> None:
    """2 real + 2 partial = 3.0 fail credit ≥ 3 → DEGRADED."""
    drv = _Driver()
    state = _bare_state()
    for partial in (False, False, True, True):
        state["peers"]["claude"]["last_tick_productive_no_handoff"] = partial
        drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["recent_fails"] == 3.0
    assert state["peers"]["claude"]["state"] == "degraded"


def test_success_clears_degraded_after_recovery() -> None:
    drv = _Driver()
    state = _bare_state()
    # Reach DEGRADED via 3 real fails
    for _ in range(3):
        state["peers"]["claude"]["last_tick_productive_no_handoff"] = False
        drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["state"] == "degraded"
    # One successful tick → healthy
    state["peers"]["claude"]["last_tick_productive_no_handoff"] = False
    drv._update_peer_health(state, "claude", success=True)
    assert state["peers"]["claude"]["state"] == "healthy"
