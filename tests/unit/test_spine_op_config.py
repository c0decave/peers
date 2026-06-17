"""STEP-3 — op-config intake: schema, validation, first ledger row.

The operator picks the *mode* and *budget*, never the goal — the goal emerges
from the bar (decision §1.4). So `from_dict` validates `mode` against the allowed
set, rejects unknown keys AND an explicit deny-list (goal / charter / any
gate-relaxation key), and `load_op_config` records the config as the FIRST
ledger row (`run-start`, witness kind `op-config`).

Covers happy (defaults + budget/dry_n overrides), edge (every allowed mode;
first-row logging), sad (missing/invalid mode, unknown key, each deny-listed key,
bad budget sub-key, non-positive dry_n, non-dict input, double-load).
"""
import pytest

from peers.spine.ledger import RunLedger
from peers.spine.op_config import ALLOWED_MODES, OpConfig, load_op_config


def test_valid_op_config_defaults():
    c = OpConfig.from_dict({"mode": "develop"})
    assert c.mode == "develop" and c.landing == "branch-pr"
    assert c.evolve_features is False and c.budget.max_rounds == 12
    assert c.budget.max_tokens is None
    assert c.dry_n == 3                       # the stop-on-dry threshold modes consume


def test_budget_and_dry_n_overrides():
    c = OpConfig.from_dict({"mode": "research", "budget": {"max_rounds": 5,
                            "max_tokens": 1000}, "dry_n": 2, "evolve_features": True})
    assert c.budget.max_rounds == 5 and c.budget.max_tokens == 1000
    assert c.dry_n == 2 and c.evolve_features is True


@pytest.mark.parametrize("mode", ["develop", "find-bugs:reproduce",
                                  "research", "bring-up"])
def test_every_allowed_mode_parses(mode):
    assert OpConfig.from_dict({"mode": mode}).mode == mode


def test_rejects_goal_or_gate_relaxation():
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "goal": "do X"})
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "disable_gate": "fail-first"})
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "trust_marker": "x"})


@pytest.mark.parametrize("key", ["goal", "charter", "disable_gate", "trust_marker",
                                 "proof_oracle", "skip_gate", "allow_unattested"])
def test_each_deny_listed_key_is_rejected(key):
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", key: "x"})


def test_missing_mode_is_rejected():
    with pytest.raises(ValueError):
        OpConfig.from_dict({})


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "delete-everything"})


def test_find_bugs_hunt_is_not_an_allowed_mode():
    # FB-06/SPEC-08: find-bugs:hunt was an allowed label with NO frontend/builder
    # anywhere -- an allowed-but-unrunnable mode is a dishonest surface. It is
    # removed; requesting it now fails CLOSED as an unknown mode, exactly like any
    # other unimplemented label.
    assert "find-bugs:hunt" not in ALLOWED_MODES
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "find-bugs:hunt"})


def test_unknown_top_level_key_is_rejected():
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "wat": 1})


def test_unknown_budget_sub_key_is_rejected():
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "budget": {"max_spend": 9}})


def test_non_positive_dry_n_is_rejected():
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "dry_n": 0})
    with pytest.raises(ValueError):
        OpConfig.from_dict({"mode": "develop", "dry_n": -1})


def test_non_dict_input_is_rejected():
    with pytest.raises((ValueError, TypeError)):
        OpConfig.from_dict("mode=develop")


def test_op_config_is_first_ledger_row(tmp_path):
    c = OpConfig.from_dict({"mode": "find-bugs:reproduce"})
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(c, led, mode_run="r1")
    rows = led.read()
    assert rows[0].event == "run-start" and rows[0].witness["kind"] == "op-config"
    assert rows[0].mode_run == "r1"
    assert rows[0].witness["mode"] == "find-bugs:reproduce"


def test_load_op_config_refuses_non_first_row(tmp_path):
    # sad: the op-config MUST be the first row; loading onto a non-empty ledger
    # is a misuse and fails closed.
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="dry-round", status="dry")
    with pytest.raises(ValueError):
        load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
