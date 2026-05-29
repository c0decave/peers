from peers.turn_manager import TurnManager


def _fresh_state(turn: str = "claude", order=None,
                 degraded: list | None = None) -> dict:
    order = list(order) if order is not None else ["claude", "codex"]
    degraded = degraded or []
    return {
        "peer_order": order,
        "turn_index": order.index(turn),
        "peers": {
            n: {
                "consecutive_fails": 0,
                "state": "degraded" if n in degraded else "healthy",
            } for n in order
        },
    }


def test_current_returns_state_value():
    state = _fresh_state("claude")
    tm = TurnManager(state, max_retries=2)
    assert tm.current() == "claude"


def test_advance_on_success_flips_turn():
    state = _fresh_state("claude")
    state["peers"]["claude"]["consecutive_fails"] = 1
    tm = TurnManager(state, max_retries=2)
    tm.advance(success=True)
    assert state["peer_order"][state["turn_index"]] == "codex"
    assert state["peers"]["claude"]["consecutive_fails"] == 0


def test_advance_on_failure_keeps_turn_until_max_retries():
    state = _fresh_state("claude")
    tm = TurnManager(state, max_retries=2)
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "claude"
    assert state["peers"]["claude"]["consecutive_fails"] == 1
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "claude"
    tm.advance(success=False)  # third failure exceeds max_retries
    assert state["peer_order"][state["turn_index"]] == "codex"


def test_default_max_retries_is_one_not_two():
    """Bug B: default rotation was max_retries=2 (3 consec fails needed).
    v12 wasted ~67min on claude struggling 3 ticks before rotation. New
    default rotates after 2 consec fails (max_retries=1) so the OTHER
    peer gets a chance to attack the same problem from a fresh angle."""
    state = _fresh_state("claude")
    tm = TurnManager(state)  # no explicit max_retries → uses new default
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "claude"
    assert state["peers"]["claude"]["consecutive_fails"] == 1
    tm.advance(success=False)  # second failure now exceeds max_retries=1
    assert state["peer_order"][state["turn_index"]] == "codex"


def test_max_retries_read_from_state_config():
    """max_retries can be overridden per-project via state['config']['max_retries'].

    Lets audit-mode operators bump retries up (peer needs more time to think)
    or down (aggressive diversification) without changing the substrate default.
    """
    state = _fresh_state("claude")
    state["config"] = {"max_retries": 3}
    tm = TurnManager.from_state(state)  # factory honors config
    for _ in range(3):
        tm.advance(success=False)
    # 3 fails not yet > max_retries=3 → still claude
    assert state["peer_order"][state["turn_index"]] == "claude"
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "codex"


def test_from_state_uses_default_when_config_missing():
    state = _fresh_state("claude")
    tm = TurnManager.from_state(state)  # no state['config'] at all
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "claude"
    tm.advance(success=False)  # new default 1 → rotate
    assert state["peer_order"][state["turn_index"]] == "codex"


def test_current_skips_degraded_peer_if_healthy_exists(monkeypatch):
    """degraded peer at turn_index → current() hops forward
    to the next healthy peer and pins turn_index there."""
    state = _fresh_state("claude", degraded=["claude"])
    tm = TurnManager(state, max_retries=2)
    assert tm.current() == "codex"
    # turn_index was advanced as a side effect
    assert state["peer_order"][state["turn_index"]] == "codex"


def test_current_skips_consecutive_degraded(monkeypatch):
    """n=3 with two degraded peers at the front — current() must skip
    both and land on the healthy one."""
    state = _fresh_state(
        "claude",
        order=["claude", "claude-2", "claude-3"],
        degraded=["claude", "claude-2"],
    )
    tm = TurnManager(state, max_retries=2)
    assert tm.current() == "claude-3"


def test_current_falls_back_when_all_degraded():
    """If ALL peers are degraded, no skipping happens — the driver's
    HALT-all-degraded path is responsible for ending the loop."""
    state = _fresh_state("claude", degraded=["claude", "codex"])
    tm = TurnManager(state, max_retries=2)
    # Should NOT crash, returns the rotation target unchanged.
    assert tm.current() == "claude"


def test_current_handles_missing_state_field():
    """Legacy state.json without per-peer `state` (only
    consecutive_fails) must not regress — treat as healthy."""
    state = {
        "peer_order": ["claude", "codex"],
        "turn_index": 0,
        "peers": {n: {"consecutive_fails": 0} for n in ("claude", "codex")},
    }
    tm = TurnManager(state, max_retries=2)
    assert tm.current() == "claude"


def test_other_returns_opposite():
    state = _fresh_state("claude")
    tm = TurnManager(state, max_retries=2)
    assert tm.other() == "codex"
    state["turn_index"] = 1
    assert tm.other() == "claude"


def test_forced_flip_resets_failed_peers_counter():
    """After a peer exhausts retries and control is forced to the other,
    the failed peer's counter must reset — otherwise next time the
    turn returns it has zero retries."""
    state = _fresh_state("claude")
    tm = TurnManager(state, max_retries=2)
    # 3 failures: 1, 2, 3 (last one trips the flip)
    tm.advance(success=False)
    tm.advance(success=False)
    tm.advance(success=False)
    assert state["peer_order"][state["turn_index"]] == "codex"
    assert state["peers"]["claude"]["consecutive_fails"] == 0, \
        "claude's counter must be cleared so it gets full retries next turn"


def test_round_robin_n3():
    """n=3 must rotate cleanly through all three peers."""
    state = _fresh_state("claude", order=["claude", "codex", "claude-2"])
    tm = TurnManager(state, max_retries=2)
    seen = [tm.current()]
    for _ in range(5):
        tm.advance(success=True)
        seen.append(tm.current())
    assert seen == [
        "claude", "codex", "claude-2",
        "claude", "codex", "claude-2",
    ]


def test_others_for_n3_returns_two():
    state = _fresh_state("codex", order=["claude", "codex", "claude-2"])
    tm = TurnManager(state, max_retries=2)
    assert tm.others() == ["claude", "claude-2"]


def test_other_raises_on_n_not_2():
    """`other()` is an n=2 shortcut and must refuse n>2 to prevent
    silent miscounts."""
    import pytest
    state = _fresh_state("a", order=["a", "b", "c"])
    tm = TurnManager(state, max_retries=2)
    with pytest.raises(ValueError):
        tm.other()
