"""Test two-phase convergence (Task 6.5)."""
from __future__ import annotations

from peers.driver_orchestrator import _resolve_convergence_state


def test_non_implement_mode_unchanged():
    """Non-implement modes pass current_phase through unchanged."""
    for mode in ["audit", "thorough", "security-owasp-web", ""]:
        for cp in ["A", "B", "complete"]:
            assert _resolve_convergence_state(mode, cp, 5, 5, 2, 2) == cp


def test_implement_phase_a_not_yet_reached():
    assert _resolve_convergence_state("implement", "A", 3, 5, 2, 0) == "A"


def test_implement_phase_a_just_cleared():
    assert _resolve_convergence_state("implement", "A", 5, 5, 2, 0) == "B"


def test_implement_phase_a_well_past():
    """Even with extra clean ticks beyond N, transition still happens once."""
    assert _resolve_convergence_state("implement", "A", 10, 5, 2, 0) == "B"


def test_implement_phase_b_not_yet_done():
    assert _resolve_convergence_state("implement", "B", 5, 5, 2, 1) == "B"


def test_implement_phase_b_just_cleared():
    assert _resolve_convergence_state("implement", "B", 5, 5, 2, 2) == "complete"


def test_implement_complete_stays_complete():
    assert _resolve_convergence_state("implement", "complete", 0, 5, 2, 0) == "complete"


def test_implement_default_n_values():
    """If only the helper is called without phase_a_n/phase_b_n, must be configurable."""
    # Phase A defaults
    assert _resolve_convergence_state("implement", "A", 5, 5, 2, 0) == "B"
    # Phase B defaults
    assert _resolve_convergence_state("implement", "B", 5, 5, 2, 2) == "complete"
