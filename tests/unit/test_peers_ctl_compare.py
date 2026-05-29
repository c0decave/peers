"""Item 11: peers-ctl compare — cross-run metrics aggregation."""
from __future__ import annotations

import json
from pathlib import Path

from peers_ctl.compare import (
    collect_project_metrics,
    render_comparison,
)


def _seed_state(proj: Path, *, iteration: int, runtime_s: int,
                max_runtime_s: int, wasted_s: int,
                clean_ticks: int, convergence_n: int = 3,
                stop_reason: str = "") -> None:
    peers = proj / ".peers"
    peers.mkdir(parents=True, exist_ok=True)
    (peers / "state.json").write_text(json.dumps({
        "iteration": iteration,
        "consecutive_clean_ticks": clean_ticks,
        "budget": {
            "spent_runtime_s": runtime_s,
            "max_runtime_s": max_runtime_s,
            "spent_iterations": iteration,
            "wasted_runtime_s": wasted_s,
            "spent_tokens": 12345,
            "spent_usd": 0.42,
        },
        "config": {"goals": {"convergence_n": convergence_n}},
    }))
    if stop_reason:
        (peers / "last-stop-reason.txt").write_text(f"{stop_reason}\n")


def _seed_runs(proj: Path, entries: list[dict]) -> None:
    log_dir = proj / ".peers" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n"
    )


def _seed_bugs(proj: Path, bugs: list[dict]) -> None:
    bugs_dir = proj / ".peers" / "bugs"
    bugs_dir.mkdir(parents=True, exist_ok=True)
    for i, b in enumerate(bugs, start=1):
        header = json.dumps(b)
        (bugs_dir / f"BUG-{i:03d}.md").write_text(
            f"{header}\n\n# {b.get('title', '?')}\n",
        )


def test_collect_minimal_state(tmp_path: Path) -> None:
    proj = tmp_path / "v11"
    _seed_state(proj, iteration=13, runtime_s=18949, max_runtime_s=21600,
                wasted_s=8340, clean_ticks=2)
    m = collect_project_metrics("v11", proj)
    assert m.iteration == 13
    assert m.spent_runtime_s == 18949
    assert m.max_runtime_s == 21600
    assert m.wasted_runtime_s == 8340
    assert m.consecutive_clean_ticks == 2
    assert m.bugs_total == 0
    assert m.ticks_to_convergence is None  # 2 < convergence_n=3


def test_collect_with_bugs_and_runs(tmp_path: Path) -> None:
    proj = tmp_path / "v12"
    _seed_state(proj, iteration=12, runtime_s=10467, max_runtime_s=43200,
                wasted_s=4008, clean_ticks=3, stop_reason="complete")
    _seed_runs(proj, [
        {"iteration": 1, "peer": "claude", "classification": "success",
         "success": True, "peer_state_after": "healthy"},
        {"iteration": 2, "peer": "codex", "classification": "success",
         "success": True, "peer_state_after": "healthy"},
        {"iteration": 9, "peer": "claude", "classification": "success",
         "success": False, "peer_state_after": "healthy"},  # no-handoff
        {"iteration": 11, "peer": "claude", "classification": "idle-timeout",
         "success": False, "peer_state_after": "degraded"},
    ])
    _seed_bugs(proj, [
        {"id": "BUG-200", "severity": "crit"},
        {"id": "BUG-201", "severity": "high"},
        {"id": "BUG-202", "severity": "med"},
    ])
    m = collect_project_metrics("v12", proj)
    assert m.iteration == 12
    assert m.success_ticks == 2
    assert m.no_handoffs == 1
    assert m.idle_timeouts == 1
    assert m.degraded_events == 1
    assert m.bugs_total == 3
    assert m.bugs_by_severity == {"crit": 1, "high": 1, "med": 1}
    assert m.ticks_to_convergence == 10  # iter 12 - n_needed(3) + 1
    assert m.stop_reason == "complete"


def test_render_two_project_table(tmp_path: Path) -> None:
    v11 = tmp_path / "v11"
    v12 = tmp_path / "v12"
    _seed_state(v11, iteration=13, runtime_s=18949, max_runtime_s=21600,
                wasted_s=8340, clean_ticks=0, stop_reason="peer-unavailable")
    _seed_state(v12, iteration=12, runtime_s=10467, max_runtime_s=43200,
                wasted_s=4008, clean_ticks=3, stop_reason="complete")
    m11 = collect_project_metrics("v11", v11)
    m12 = collect_project_metrics("v12", v12)
    out = render_comparison([m11, m12])
    # Header includes both project names
    assert "v11" in out and "v12" in out
    # Key metrics surface
    assert "iteration" in out
    assert "wasted" in out
    assert "stop reason" in out
    assert "peer-unavailable" in out
    assert "complete" in out


def test_render_handles_missing_state(tmp_path: Path) -> None:
    """Project dir without .peers/ still renders (zeroed)."""
    proj = tmp_path / "empty"
    proj.mkdir()
    m = collect_project_metrics("empty", proj)
    out = render_comparison([m])
    assert "empty" in out
    assert "iteration" in out


def test_render_with_zero_projects() -> None:
    out = render_comparison([])
    assert "no projects" in out
