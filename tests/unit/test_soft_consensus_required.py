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


def test_malformed_config_containers_default_true_sad_path() -> None:
    for state in (
        {"config": ["not-a-mapping"]},
        {"config": {"goals": ["not-a-mapping"]}},
    ):
        assert soft_consensus_required_for_convergence(state) is True


def test_empty_string_value_treated_as_false_edge() -> None:
    # edge: explicit empty-string in YAML is in _SOFT_CONSENSUS_FALSY
    # alongside "false"/"0"/"no"/"off" — so a half-written config like
    # `soft_consensus_required: ""` disables strict mode, NOT the
    # opposite. Pin the boundary to surface a future tightening as a
    # deliberate decision.
    state = {"config": {"goals": {"soft_consensus_required": ""}}}
    assert soft_consensus_required_for_convergence(state) is False


def test_zero_int_value_treated_as_false_edge() -> None:
    # edge: a numeric 0 (legacy yaml int → bool coercion) is falsey via
    # the final `bool(raw)` fallthrough — `0` does not match the string
    # check and `False` is not an instance of bool? Actually it is.
    # Confirm 0 (a non-bool/non-str truthy-ish value) collapses to False
    # so an unexpected type doesn't flip the safety default open.
    state = {"config": {"goals": {"soft_consensus_required": 0}}}
    assert soft_consensus_required_for_convergence(state) is False


def test_uppercase_string_value_is_case_insensitive_edge() -> None:
    # edge: a YAML scalar like "FALSE" or "NO" must be normalised so
    # the operator's case doesn't change semantics — the implementation
    # `.strip().lower()` handles this; pin the contract.
    for val in ("FALSE", "False", " no ", "OFF"):
        state = {"config": {"goals": {"soft_consensus_required": val}}}
        assert soft_consensus_required_for_convergence(state) is False, val
