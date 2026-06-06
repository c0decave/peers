"""Rate-limit health/rotation handling + degraded-peer recovery (v17 finding).

Companion to test_rate_limit_handling.py (which covers the classification).
Here: a `rate-limited` tick must NOT degrade the peer (Fix 3a) and must rotate
to the other peer without penalty (Fix 2 — natural backoff via the other peer's
tick + a small explicit backoff against a hot spin). And a peer that DID get
degraded must get a periodic recovery turn instead of being benched forever
while a healthy peer exists (Fix 3b — anti-starvation).
"""
from __future__ import annotations

from peers.driver_peer_health import DriverPeerHealthMixin
from peers.rate_limit import rate_limit_backoff_s
from peers.turn_manager import TurnManager


class _Health(DriverPeerHealthMixin):
    def __init__(self) -> None:
        pass


def _bare_state(peer: str = "claude") -> dict:
    return {
        "peers": {peer: {"state": "healthy", "consecutive_fails": 0,
                         "recent_fails": 0, "recent_runs": []}},
        "iteration": 0,
    }


def _two_peers(turn_index: int = 0, iteration: int = 0,
               claude_state: str = "healthy",
               degraded_at_iter: int | None = None) -> dict:
    claude = {"state": claude_state, "consecutive_fails": 0}
    if degraded_at_iter is not None:
        claude["degraded_at_iter"] = degraded_at_iter
    return {
        "peer_order": ["claude", "codex"],
        "turn_index": turn_index,
        "iteration": iteration,
        "peers": {
            "claude": claude,
            "codex": {"state": "healthy", "consecutive_fails": 0},
        },
    }


# --- Fix 3a: rate-limited does not degrade ---------------------------------

def test_rate_limited_ticks_do_not_degrade() -> None:
    drv = _Health()
    state = _bare_state()
    # Five transient rate-limits in a row must NOT push the peer to degraded.
    for _ in range(5):
        drv._update_peer_health(state, "claude", success=False,
                                rate_limited=True)
    assert state["peers"]["claude"]["recent_fails"] == 0.0
    assert state["peers"]["claude"]["state"] == "healthy"


def test_real_fail_still_degrades_after_rate_limits() -> None:
    """Rate-limits are neutral, but genuine fails still degrade (no masking)."""
    drv = _Health()
    state = _bare_state()
    for _ in range(2):
        drv._update_peer_health(state, "claude", success=False,
                                rate_limited=True)
    for _ in range(3):
        drv._update_peer_health(state, "claude", success=False)
    assert state["peers"]["claude"]["state"] == "degraded"


# --- Fix 2: rotate without penalty on rate-limited -------------------------

def test_advance_rate_limited_rotates_without_penalty() -> None:
    state = _two_peers(turn_index=0)
    tm = TurnManager(state)
    tm.advance(success=False, rate_limited=True)
    assert state["turn_index"] == 1            # rotated to the other peer
    assert state["peers"]["claude"]["consecutive_fails"] == 0  # no penalty


def test_rate_limit_backoff_grows_and_caps() -> None:
    vals = [rate_limit_backoff_s(n) for n in range(1, 8)]
    assert vals == sorted(vals)                # non-decreasing
    assert vals[0] >= 1                        # first retry waits a little
    assert all(v <= 120 for v in vals)         # capped
    assert rate_limit_backoff_s(0) == 0        # no streak -> no wait


# --- Fix 3b: degraded peer recovery (anti-starvation) ----------------------

def test_degraded_peer_skipped_during_cooldown() -> None:
    # claude degraded at iter 0; only 3 ticks elapsed (< recovery interval).
    state = _two_peers(turn_index=0, iteration=3,
                       claude_state="degraded", degraded_at_iter=0)
    tm = TurnManager(state)
    assert tm.current() == "codex"             # claude still benched


def test_degraded_peer_gets_recovery_turn_after_cooldown() -> None:
    state = _two_peers(turn_index=0, iteration=8,
                       claude_state="degraded", degraded_at_iter=0)
    tm = TurnManager(state)
    # Cooldown elapsed -> claude is eligible for a recovery attempt again.
    assert tm.current() == "claude"


def test_failed_recovery_restarts_cooldown() -> None:
    drv = _Health()
    state = _bare_state()
    state["peers"]["claude"]["state"] = "degraded"
    state["peers"]["claude"]["degraded_at_iter"] = 0
    state["peers"]["claude"]["recent_runs"] = [False, False, False]
    state["peers"]["claude"]["recent_fails"] = 3.0
    state["iteration"] = 8
    drv._update_peer_health(state, "claude", success=False)  # recovery failed
    # Cooldown restarts so it is benched again, not retried every tick — but
    # the immutable first-degradation marker is NOT disturbed.
    assert state["peers"]["claude"]["recovery_cooldown_iter"] == 8
    assert state["peers"]["claude"]["degraded_at_iter"] == 0


def test_successful_recovery_returns_to_healthy() -> None:
    drv = _Health()
    state = _bare_state()
    state["peers"]["claude"]["state"] = "degraded"
    state["peers"]["claude"]["degraded_at_iter"] = 0
    state["peers"]["claude"]["recent_runs"] = [False, False, False]
    state["iteration"] = 8
    drv._update_peer_health(state, "claude", success=True)
    assert state["peers"]["claude"]["state"] == "healthy"


def test_heal_clears_recovery_cooldown_marker() -> None:
    """I4 regression: a successful recovery must clear recovery_cooldown_iter,
    else a later re-degradation inherits the STALE marker and is granted an
    immediate recovery turn instead of waiting the full cooldown (premature
    retry — the opposite of the starvation bug)."""
    drv = _Health()
    state = _bare_state()
    state["peers"]["claude"].update({
        "state": "degraded", "degraded_at_iter": 5,
        "recovery_cooldown_iter": 13, "recent_runs": [False, False, False],
    })
    state["iteration"] = 21
    drv._update_peer_health(state, "claude", success=True)
    assert state["peers"]["claude"]["state"] == "healthy"
    assert "recovery_cooldown_iter" not in state["peers"]["claude"]


def test_freshly_redegraded_peer_waits_full_cooldown() -> None:
    """After a heal clears the stale marker, a freshly re-degraded peer is
    benched for the full recovery_interval (not retried immediately)."""
    state = _two_peers(turn_index=0, iteration=31,
                       claude_state="degraded", degraded_at_iter=30)
    # No recovery_cooldown_iter (cleared on the prior heal).
    tm = TurnManager(state)
    assert tm.current() == "codex"  # benched: 31 - 30 = 1 < interval (8)
