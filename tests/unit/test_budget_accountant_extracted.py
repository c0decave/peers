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


def test_read_operator_budget_overrides_rejects_oversize_file_BUG_516(tmp_path):
    """BUG-516: a huge (e.g. same-UID-swapped) budget-overrides.json must be
    rejected wholesale, not read + json.loads'd unbounded (memory DoS). The
    read is capped at _BUDGET_OVERRIDE_MAX_BYTES; over-cap content yields {}."""
    import json
    from pathlib import Path
    from peers.budget_accountant import (
        read_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
        _BUDGET_OVERRIDE_MAX_BYTES,
    )

    repo = Path(tmp_path)
    (repo / ".peers").mkdir()
    payload = {"max_runtime_s": 999, "_pad": "x" * _BUDGET_OVERRIDE_MAX_BYTES}
    (repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE).write_text(
        json.dumps(payload), encoding="utf-8",
    )
    assert read_operator_budget_overrides(repo) == {}


def test_read_operator_budget_overrides_accepts_normal_after_cap_BUG_516(tmp_path):
    """A normal small override still works with the cap in place."""
    import json
    from pathlib import Path
    from peers.budget_accountant import (
        read_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
    )

    repo = Path(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE).write_text(
        json.dumps({"max_runtime_s": 43200}), encoding="utf-8",
    )
    assert read_operator_budget_overrides(repo) == {"max_runtime_s": 43200}


def test_read_operator_budget_overrides_refuses_symlinked_leaf_BUG_513(
    tmp_path,
):
    """BUG-513 (defense-in-depth, sad path): the inner orchestrator's read
    of `.peers/budget-overrides.json` must refuse a same-UID symlink swap,
    matching the safe_io pattern the rest of the substrate uses for every
    other `.peers/*` read (BUG-185/198/224/238/240 family). A prompt-
    injected peer that swaps the file for a symlink to attacker-staged
    JSON outside `.peers/` would otherwise silently override operator
    budget caps (`--max-usd`, `--max-runtime`) at the next loop start —
    the controller-side write (`_persist_budget_override`) was hardened
    by BUG-238 but this reader was not."""
    import json
    from pathlib import Path
    from peers.budget_accountant import (
        read_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
    )

    repo = Path(tmp_path) / "proj"
    repo.mkdir()
    (repo / ".peers").mkdir()
    attacker = Path(tmp_path) / "attacker.json"
    attacker.write_text(json.dumps({"max_usd": 9999.99}))
    leaf = repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE
    leaf.symlink_to(attacker)

    # Pre-fix: plain `path.read_text()` follows the symlink and surfaces
    # attacker-controlled max_usd. Post-fix: safe_io refuses the symlinked
    # leaf and returns {} so the operator's real cap survives.
    assert read_operator_budget_overrides(repo) == {}


def test_read_operator_budget_overrides_deep_json_returns_empty_BUG_514(
    tmp_path,
):
    """BUG-514: deeply nested malformed JSON must fail closed.

    `json.loads()` raises RecursionError rather than JSONDecodeError for
    sufficiently deep arrays, and the operator-override reader promises {}
    for malformed sidecars instead of crashing orchestration startup.
    """
    from pathlib import Path
    from peers.budget_accountant import (
        read_operator_budget_overrides,
        OPERATOR_BUDGET_OVERRIDE_FILE,
    )

    repo = Path(tmp_path)
    (repo / ".peers").mkdir()
    (repo / ".peers" / OPERATOR_BUDGET_OVERRIDE_FILE).write_text(
        "[" * 10000, encoding="utf-8",
    )

    assert read_operator_budget_overrides(repo) == {}


def test_parse_codex_tokens_from_json_usage():
    """Option C (codex --json): tokens come from the `turn.completed` event's
    `usage` (input+output), summed across turns — verified against codex-cli
    0.133's JSONL schema."""
    from peers.budget_accountant import _parse_codex_tokens
    jsonl = (
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":11035,'
        '"output_tokens":23}}\n'
    )
    tok, usd = _parse_codex_tokens(jsonl)
    assert tok == 11058
    assert usd == 0.0


def test_parse_codex_tokens_text_fallback_still_works():
    """Plain `codex exec` (no --json) still scrapes `tokens used\\n<N>`."""
    from peers.budget_accountant import _parse_codex_tokens
    assert _parse_codex_tokens("tokens used\n999\n") == (999, 0.0)


def test_parse_opencode_tokens_from_json_step_finish():
    """opencode --format json: tokens+cost come from `step-finish` part
    events (verified against opencode 1.15.13)."""
    from peers.budget_accountant import _parse_opencode_tokens
    jsonl = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","part":{"type":"tool","tool":"write"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish","tokens":'
        '{"total":10546,"input":10457,"output":69,"reasoning":20,'
        '"cache":{"write":0,"read":0}},"cost":0.0021}}\n'
        '{"type":"text","part":{"type":"text","text":"done"}}\n'
    )
    tok, usd = _parse_opencode_tokens(jsonl)
    assert tok == 10546
    assert abs(usd - 0.0021) < 1e-9


def test_parse_opencode_tokens_sums_multiple_steps():
    from peers.budget_accountant import _parse_opencode_tokens
    jsonl = (
        '{"type":"step_finish","part":{"type":"step-finish",'
        '"tokens":{"total":100},"cost":0.01}}\n'
        '{"type":"step_finish","part":{"type":"step-finish",'
        '"tokens":{"total":250},"cost":0.02}}\n'
    )
    tok, usd = _parse_opencode_tokens(jsonl)
    assert tok == 350
    assert abs(usd - 0.03) < 1e-9


def test_parse_opencode_tokens_empty_returns_zero():
    from peers.budget_accountant import _parse_opencode_tokens
    assert _parse_opencode_tokens("not json\n{bad") == (0, 0.0)


def test_rate_limited_tick_is_neutral_for_consecutive_failures():
    # full-depth-analysis #6: a transient rate-limited tick must NOT count toward
    # budget consecutive_failures (else an all-peers outage halts the run the v17
    # design exists to survive). Wall-clock still counts; wasted-runtime does not.
    state = _state()
    state["budget"]["consecutive_failures"] = 0
    base_iter = state["budget"]["spent_iterations"]
    for _ in range(7):                       # 7 > default max_consecutive_failures (5)
        record_tick_accounting(state, success=False, tick_dt=3, peer="claude",
                               rate_limited=True)
    assert state["budget"]["consecutive_failures"] == 0          # never accumulates
    assert state["budget"]["spent_iterations"] == base_iter + 7  # wall-clock counts
    assert state["budget"].get("wasted_runtime_s", 0) == 0       # not a wasted fail
    # a genuine (non-rate-limited) failure still increments
    record_tick_accounting(state, success=False, tick_dt=3, peer="claude")
    assert state["budget"]["consecutive_failures"] == 1
