"""Test blind-review tick orchestration (Task 6.2)."""
from __future__ import annotations

from peers.driver_orchestrator import _load_phase_prompt, _resolve_peer_role


def test_non_implement_mode_always_normal():
    assert _resolve_peer_role("audit", "implementation", 0) == "normal"
    assert _resolve_peer_role("thorough", "implementation", 5) == "normal"
    assert _resolve_peer_role("security-owasp-web", "implementation", 99) == "normal"


def test_implement_phase_0_is_normal():
    """Phase 0 ticks (0/1/2) use Phase 0 prompts, not blind-review prompts."""
    assert _resolve_peer_role("implement", "recon", 0) == "normal"
    assert _resolve_peer_role("implement", "alignment", 1) == "normal"
    assert _resolve_peer_role("implement", "architecture", 2) == "normal"


def test_implement_tick_3_is_implementer():
    assert _resolve_peer_role("implement", "implementation", 3) == "implementer"


def test_implement_tick_4_is_reviewer():
    assert _resolve_peer_role("implement", "implementation", 4) == "reviewer"


def test_implement_alternates_correctly():
    for t in range(3, 20):
        role = _resolve_peer_role("implement", "implementation", t)
        expected = "implementer" if (t - 3) % 2 == 0 else "reviewer"
        assert role == expected, f"tick {t}: got {role}, expected {expected}"


def test_blind_review_prompt_overlays_exist_and_load():
    """Both implementer + reviewer prompt overlays loadable via _load_phase_prompt."""
    # The phase loader was generalized for Phase 0; reuse it
    impl = _load_phase_prompt("implement", "blind_review_implementer")
    assert impl is not None
    assert "IMPLEMENTATION_NOTES.md" in impl

    rev = _load_phase_prompt("implement", "blind_review_reviewer")
    assert rev is not None
    assert "REVIEW_NOTES.md" in rev
    assert "DO NOT read" in rev or "do not read" in rev.lower()
