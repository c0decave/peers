from __future__ import annotations

from types import SimpleNamespace

from peers.budget_accountant import (
    BudgetAccountant,
    account_tokens_usd,
    record_tick_accounting,
)


def _state() -> dict:
    return {
        "iteration": 3,
        "budget": {
            "spent_iterations": 3,
            "spent_runtime_s": 21,
            "spent_tokens": 10,
            "spent_usd": 0.5,
            "consecutive_failures": 2,
        },
    }


def test_account_tokens_usd_adds_parser_output_to_budget():
    state = _state()
    run = SimpleNamespace(
        stdout=(
            '{"type":"result","total_cost_usd":0.25,'
            '"usage":{"input_tokens":4,"cache_read_input_tokens":6,'
            '"output_tokens":8}}\n'
        ),
        stderr="",
    )

    tokens, usd = account_tokens_usd(state, "claude", run)

    assert tokens == 18
    assert usd == 0.25
    assert state["budget"]["spent_tokens"] == 28
    assert state["budget"]["spent_usd"] == 0.75


def test_budget_accountant_facade_records_tick_cost():
    accountant = BudgetAccountant(
        max_iterations=200,
        max_runtime_s=21600,
        max_tokens=None,
        max_usd=None,
    )

    accountant.record_tick(tokens=1000, usd=0.01, duration_s=120)
    state = accountant.snapshot()

    assert state["spent_tokens"] == 1000
    assert state["spent_usd"] == 0.01
    assert state["spent_runtime_s"] == 120
    assert state["spent_iterations"] == 1


def test_budget_accountant_facade_exhausted_after_runtime_cap():
    accountant = BudgetAccountant(
        max_runtime_s=100,
        max_iterations=200,
        max_tokens=None,
        max_usd=None,
    )

    accountant.record_tick(tokens=0, usd=0.0, duration_s=120)

    assert accountant.reason() == "max_runtime"
    assert accountant.is_exhausted("runtime")


def test_account_tokens_usd_unknown_tool_is_noop():
    state = _state()
    run = SimpleNamespace(stdout="tokens used\n999\n", stderr="")

    tokens, usd = account_tokens_usd(state, "custom-tool", run)

    assert (tokens, usd) == (0, 0.0)
    assert state == _state()


def test_record_tick_accounting_success_resets_consecutive_failures():
    state = _state()

    record_tick_accounting(state, success=True, tick_dt=9)

    assert state["iteration"] == 4
    assert state["budget"]["spent_iterations"] == 4
    assert state["budget"]["spent_runtime_s"] == 30
    assert state["budget"]["consecutive_failures"] == 0
    assert "wasted_runtime_s" not in state["budget"]


def test_record_tick_accounting_failure_tracks_wasted_runtime():
    state = _state()

    record_tick_accounting(state, success=False, tick_dt=9)

    assert state["iteration"] == 4
    assert state["budget"]["spent_iterations"] == 4
    assert state["budget"]["spent_runtime_s"] == 30
    assert state["budget"]["wasted_runtime_s"] == 9
    assert state["budget"]["consecutive_failures"] == 3


def test_operator_override_wins_over_config_budget(tmp_path):
    """Reproduces the --max-runtime clobber bug: `_apply_config_budget`
    overlays config.yaml's cap onto state every loop start, so an operator
    `--max-runtime` (persisted to the override sidecar) must be re-applied
    AFTER the config overlay to win. Spent counters and config-only caps
    are unaffected."""
    import json
    from pathlib import Path
    from peers.budget_accountant import (
        _apply_config_budget,
        apply_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
    )

    repo = Path(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE).write_text(
        json.dumps({"max_runtime_s": 43200})
    )
    state = {"budget": {"max_runtime_s": 43200, "spent_runtime_s": 10}}
    cfg_budget = {"max_runtime_s": 21600, "max_iterations": 200}

    _apply_config_budget(state, cfg_budget)
    # The config overlay clobbers the operator cap — this is the bug.
    assert state["budget"]["max_runtime_s"] == 21600

    applied = apply_operator_budget_overrides(state, repo)
    # The operator override is restored and wins.
    assert state["budget"]["max_runtime_s"] == 43200
    assert applied == {"max_runtime_s": 43200}
    # Config-only caps still come from config; spent counters untouched.
    assert state["budget"]["max_iterations"] == 200
    assert state["budget"]["spent_runtime_s"] == 10


def test_read_operator_budget_overrides_absent_and_malformed(tmp_path):
    """Absent / non-dict / malformed-JSON files yield {}, and only
    recognised numeric budget caps survive (unknown keys and bool values
    are dropped — bool is an int subclass and would be a footgun)."""
    import json
    from pathlib import Path
    from peers.budget_accountant import (
        read_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
    )

    repo = Path(tmp_path)
    assert read_operator_budget_overrides(repo) == {}

    (repo / ".peers").mkdir()
    f = repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE
    f.write_text(json.dumps(
        {"max_runtime_s": 43200, "unknown_key": 1, "max_iterations": True}
    ))
    assert read_operator_budget_overrides(repo) == {"max_runtime_s": 43200}

    f.write_text("{not valid json")
    assert read_operator_budget_overrides(repo) == {}
