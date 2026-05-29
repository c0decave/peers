"""Item 8: soft-consensus separate convergence requirement.

v9 + v10 both ended budget:max_runtime (not 'complete') because
_all_green_including_soft required every soft goal to reach peer-review
consensus, even after all hard gates were green for many ticks. Operators
who want hard-gate-driven convergence can now set
.peers/config.yaml -> goals.soft_consensus_required: false.

The default (true) preserves the legacy strict semantics.
"""
from __future__ import annotations

from peers.driver_soft_reviews import soft_consensus_required_for_convergence


def test_default_true_for_backwards_compat() -> None:
    assert soft_consensus_required_for_convergence({}) is True


def test_explicit_true_passes_through() -> None:
    state = {"config": {"goals": {"soft_consensus_required": True}}}
    assert soft_consensus_required_for_convergence(state) is True


def test_explicit_false_skips_soft() -> None:
    state = {"config": {"goals": {"soft_consensus_required": False}}}
    assert soft_consensus_required_for_convergence(state) is False


def test_truthy_string_treated_as_true() -> None:
    state = {"config": {"goals": {"soft_consensus_required": "yes"}}}
    # Strings other than "false"/"0"/"no"/"" count as True.
    assert soft_consensus_required_for_convergence(state) is True


def test_falsy_string_treated_as_false() -> None:
    state = {"config": {"goals": {"soft_consensus_required": "false"}}}
    assert soft_consensus_required_for_convergence(state) is False


def test_missing_goals_section_defaults_true() -> None:
    state = {"config": {}}
    assert soft_consensus_required_for_convergence(state) is True


def test_no_config_at_all_defaults_true() -> None:
    state: dict = {}
    assert soft_consensus_required_for_convergence(state) is True
