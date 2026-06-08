"""Regression tests for the `peers-ctl compare` adapter (cmd_compare).

The `compare` subcommand was wired into cli.py (argparser + dispatch)
but cmd_compare() was never defined, so every invocation raised
NameError (ruff F821). These tests exercise the implemented adapter:
name resolution, the happy table render, and the under-resolved guards.
"""
from __future__ import annotations

import json
from pathlib import Path

from peers_ctl.compare import cmd_compare, collect_project_metrics


def _bootstrap_project(root: Path, name: str, *, iteration: int) -> None:
    """Minimal on-disk project: a .peers/state.json under root/name."""
    peers = root / name / ".peers"
    peers.mkdir(parents=True, exist_ok=True)
    (peers / "state.json").write_text(
        json.dumps({
            "iteration": iteration,
            "budget": {"spent_runtime_s": 10, "spent_tokens": 5},
        }),
        encoding="utf-8",
    )


def test_cmd_compare_renders_table_for_bare_name_projects(
    tmp_path: Path, monkeypatch, capsys,
):
    """cmd_compare resolves bare names under PEERS_PROJECTS_ROOT and prints
    a table. Regression for the F821 where the `compare` subcommand was
    wired in cli.py but cmd_compare() was never defined."""
    root = tmp_path / "root"
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(root))
    _bootstrap_project(root, "runA", iteration=5)
    _bootstrap_project(root, "runB", iteration=9)

    rc = cmd_compare(["runA", "runB"], config_dir=tmp_path / "ctl")

    assert rc == 0
    out = capsys.readouterr().out
    assert "runA" in out and "runB" in out
    assert "iteration" in out


def test_cmd_compare_refuses_fewer_than_two_names(tmp_path: Path, capsys):
    rc = cmd_compare(["solo"], config_dir=tmp_path / "ctl")

    assert rc == 2
    assert "at least 2" in capsys.readouterr().err


def test_cmd_compare_reports_missing_project(
    tmp_path: Path, monkeypatch, capsys,
):
    root = tmp_path / "root"
    monkeypatch.setenv("PEERS_PROJECTS_ROOT", str(root))
    _bootstrap_project(root, "present", iteration=1)

    rc = cmd_compare(["present", "absent"], config_dir=tmp_path / "ctl")

    assert rc == 2
    err = capsys.readouterr().err
    assert "no such project: absent" in err


def test_collect_metrics_minimal_baseline_state_renders_zeros(tmp_path: Path):
    """Happy path: a freshly bootstrapped project with default budget renders
    zero-valued numeric metrics without raising. kind: happy
    """
    proj = tmp_path / "fresh"
    peers = proj / ".peers"
    peers.mkdir(parents=True)
    (peers / "state.json").write_text(json.dumps({
        "iteration": 0,
        "budget": {},
    }), encoding="utf-8")

    m = collect_project_metrics("fresh", proj)

    assert m.iteration == 0
    assert m.spent_runtime_s == 0
    assert m.max_runtime_s is None
    assert m.spent_tokens == 0
    assert m.spent_usd == 0.0
    assert m.bugs_total == 0
    assert m.ticks_to_convergence is None


def test_collect_metrics_handles_non_integer_state_fields_at_boundary(
    tmp_path: Path,
):
    """Edge: a corrupted state.json with non-numeric values in numeric
    fields must not crash collect_project_metrics; the offending field
    falls back to the documented default. Reproducer for BUG-403.

    Crash mode before fix: int('garbage') raises ValueError and
    propagates out of `peers-ctl compare`, taking down the cross-run
    report just because one project's state file was hand-edited or
    half-written. The function already swallows JSONDecodeError /
    missing-file / bad-float for spent_usd — non-numeric int fields
    should be treated the same way.

    kind: edge
    """
    proj = tmp_path / "corrupt"
    peers = proj / ".peers"
    peers.mkdir(parents=True)
    (peers / "state.json").write_text(json.dumps({
        "iteration": "garbage",
        "consecutive_clean_ticks": "still-bad",
        "budget": {
            "spent_runtime_s": "NaN",
            "max_runtime_s": "n/a",
            "spent_iterations": [],
            "wasted_runtime_s": {"nested": 1},
            "spent_tokens": "many",
            "spent_usd": "free",
        },
        "config": {"goals": {"convergence_n": "three"}},
    }), encoding="utf-8")

    m = collect_project_metrics("corrupt", proj)

    assert m.iteration == 0
    assert m.consecutive_clean_ticks == 0
    assert m.spent_runtime_s == 0
    assert m.max_runtime_s is None
    assert m.spent_iterations == 0
    assert m.wasted_runtime_s == 0
    assert m.spent_tokens == 0
    assert m.spent_usd == 0.0
    assert m.ticks_to_convergence is None


def test_collect_metrics_handles_non_finite_numeric_state_fields_BUG_230(
    tmp_path: Path,
):
    """Sad path: JSON non-finite numbers must not crash numeric coercion.

    Python's json.loads accepts NaN/Infinity by default. Before BUG-230,
    int(float("inf")) raised OverflowError in _safe_int, and spent_usd
    could render as $nan/$inf instead of falling back to 0. kind: sad
    """
    proj = tmp_path / "nonfinite"
    peers = proj / ".peers"
    peers.mkdir(parents=True)
    (peers / "state.json").write_text("""{
        "iteration": Infinity,
        "consecutive_clean_ticks": Infinity,
        "budget": {
            "spent_runtime_s": Infinity,
            "max_runtime_s": Infinity,
            "spent_iterations": Infinity,
            "wasted_runtime_s": Infinity,
            "spent_tokens": Infinity,
            "spent_usd": NaN
        },
        "config": {"goals": {"convergence_n": Infinity}}
    }""", encoding="utf-8")

    m = collect_project_metrics("nonfinite", proj)

    assert m.iteration == 0
    assert m.consecutive_clean_ticks == 0
    assert m.spent_runtime_s == 0
    assert m.max_runtime_s is None
    assert m.spent_iterations == 0
    assert m.wasted_runtime_s == 0
    assert m.spent_tokens == 0
    assert m.spent_usd == 0.0
    assert m.ticks_to_convergence is None


def test_collect_metrics_treats_bool_numeric_fields_as_malformed_BUG_231(
    tmp_path: Path,
):
    """Info hardening: JSON bools are not valid compare numeric metrics."""
    proj = tmp_path / "bools"
    peers = proj / ".peers"
    peers.mkdir(parents=True)
    (peers / "state.json").write_text(json.dumps({
        "iteration": True,
        "consecutive_clean_ticks": True,
        "budget": {
            "spent_runtime_s": True,
            "max_runtime_s": True,
            "spent_iterations": True,
            "wasted_runtime_s": True,
            "spent_tokens": True,
            "spent_usd": True,
        },
        "config": {"goals": {"convergence_n": True}},
    }), encoding="utf-8")

    m = collect_project_metrics("bools", proj)

    assert m.iteration == 0
    assert m.consecutive_clean_ticks == 0
    assert m.spent_runtime_s == 0
    assert m.max_runtime_s is None
    assert m.spent_iterations == 0
    assert m.wasted_runtime_s == 0
    assert m.spent_tokens == 0
    assert m.spent_usd == 0.0
    assert m.ticks_to_convergence is None


def test_collect_metrics_handles_unhashable_peer_runs_field_BUG_232(
    tmp_path: Path,
):
    """Info hardening: a corrupted runs.jsonl whose dict entry has a non-string
    (unhashable) `peer` paired with `peer_state_after == 'degraded'` must not
    crash collect_project_metrics. The pre-fix code raises TypeError from
    `(peer, st) not in peer_states_seen` because hashing a tuple containing a
    list/dict/set raises. kind: sad
    """
    proj = tmp_path / "unhash"
    peers = proj / ".peers"
    (peers / "log").mkdir(parents=True)
    (peers / "state.json").write_text(
        json.dumps({"iteration": 1, "budget": {}}), encoding="utf-8",
    )
    (peers / "log" / "runs.jsonl").write_text(
        "\n".join([
            json.dumps({"peer": [1, 2], "peer_state_after": "degraded"}),
            json.dumps({"peer": {"k": "v"}, "peer_state_after": "degraded"}),
            json.dumps({"peer": "claude", "peer_state_after": "degraded"}),
        ]) + "\n",
        encoding="utf-8",
    )

    m = collect_project_metrics("unhash", proj)

    # Only the one well-formed (string-peer, degraded) entry should count;
    # the two malformed entries are dropped instead of crashing.
    assert m.degraded_events == 1


def test_collect_metrics_max_runtime_zero_boundary_renders_without_div_zero(
    tmp_path: Path,
):
    """Edge: max_runtime_s == 0 must not trip a div-by-zero in the percent
    formatter when render_comparison runs. kind: edge
    """
    from peers_ctl.compare import render_comparison

    proj = tmp_path / "zero"
    peers = proj / ".peers"
    peers.mkdir(parents=True)
    (peers / "state.json").write_text(json.dumps({
        "iteration": 1,
        "budget": {"spent_runtime_s": 5, "max_runtime_s": 0},
    }), encoding="utf-8")

    m = collect_project_metrics("zero", proj)
    out = render_comparison([m])

    assert "zero" in out
    assert "5s" in out
