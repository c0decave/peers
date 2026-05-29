"""Item 13: n>2 peer topologies — recovery-peer activation.

config.yaml has always supported `peers: [...]` with n>=2, but third+
slots were never exercised. This test pins the minimal substrate change
needed for a 'recovery peer' topology: the recovery peer is skipped
during normal rotation as long as at least one non-recovery peer is
healthy, and gets promoted into rotation only when all non-recovery
peers are degraded.

Witness / debater roles are pure prompt-template variations and don't
need substrate changes — they ship as documentation in the help-man.
"""
from __future__ import annotations

from peers.peer_spec import PeerSpec
from peers.turn_manager import TurnManager


def _state_with_roles(
    *,
    current: str,
    peers_state: dict[str, str],
    peer_roles: dict[str, str] | None = None,
) -> dict:
    order = list(peers_state.keys())
    state = {
        "peer_order": order,
        "turn_index": order.index(current),
        "peers": {
            n: {"state": s, "consecutive_fails": 0}
            for n, s in peers_state.items()
        },
    }
    if peer_roles:
        state["peer_roles"] = peer_roles
    return state


def test_peer_spec_accepts_role_field() -> None:
    p = PeerSpec(name="claude-recovery", tool="claude",
                  argv=("claude", "-p", "{PROMPT}"),
                  prompt_mode="argv-substitute", role="recovery")
    assert p.role == "recovery"


def test_peer_spec_role_defaults_to_default() -> None:
    p = PeerSpec(name="claude", tool="claude",
                  argv=("claude", "-p", "{PROMPT}"),
                  prompt_mode="argv-substitute")
    assert p.role == "default"


def test_turn_manager_skips_recovery_when_others_healthy() -> None:
    state = _state_with_roles(
        current="claude-rec",
        peers_state={"claude": "healthy", "codex": "healthy",
                     "claude-rec": "healthy"},
        peer_roles={"claude-rec": "recovery"},
    )
    tm = TurnManager.from_state(state)
    # When turn_index points to recovery but defaults are healthy,
    # current() should hop forward to a non-recovery healthy peer.
    assert tm.current() in ("claude", "codex")
    # turn_index should be pinned to the resolved peer.
    assert state["peer_order"][state["turn_index"]] != "claude-rec"


def test_turn_manager_activates_recovery_when_defaults_degraded() -> None:
    state = _state_with_roles(
        current="claude",
        peers_state={"claude": "degraded", "codex": "degraded",
                     "claude-rec": "healthy"},
        peer_roles={"claude-rec": "recovery"},
    )
    tm = TurnManager.from_state(state)
    # All non-recovery peers degraded → recovery becomes eligible.
    assert tm.current() == "claude-rec"


def test_turn_manager_no_recovery_peer_unchanged_behavior() -> None:
    """When no recovery peer is declared, rotation is the legacy semantics."""
    state = _state_with_roles(
        current="claude",
        peers_state={"claude": "healthy", "codex": "healthy"},
    )
    tm = TurnManager.from_state(state)
    assert tm.current() == "claude"
    tm.advance(success=True)
    assert state["peer_order"][state["turn_index"]] == "codex"
