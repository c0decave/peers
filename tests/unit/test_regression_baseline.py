"""Auto-snapshot the no-prior-regression baseline at run start.

Diagnostic finding (calc v2, 2026-05-31): `no-prior-regression` returns 1
when `.peers/passing-baseline.txt` is missing, and NOTHING ever snapshots
it — so the gate permanently fails in implement-mode and sticks every run
at the convergence-wall (`stuck:no-prior-regression`). Fix: the driver
snapshots the baseline ONCE at run start (before peers touch code) when the
gate is configured and no baseline exists yet. Mid-run deletion still
fails-closed (the check keeps treating a missing baseline as failure).
"""
from __future__ import annotations

from pathlib import Path


def test_needs_snapshot_true_when_gate_present_and_baseline_missing(
    tmp_path: Path,
) -> None:
    from peers.regression_baseline import needs_baseline_snapshot
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    assert needs_baseline_snapshot(peer_dir, ["no-prior-regression", "tests-pass"])


def test_needs_snapshot_false_when_baseline_exists(tmp_path: Path) -> None:
    from peers.regression_baseline import needs_baseline_snapshot
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "passing-baseline.txt").write_text("x::test_a\n")
    assert not needs_baseline_snapshot(peer_dir, ["no-prior-regression"])


def test_needs_snapshot_false_when_gate_not_configured(tmp_path: Path) -> None:
    from peers.regression_baseline import needs_baseline_snapshot
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    # gate absent → never snapshot (audit/other modes without the gate)
    assert not needs_baseline_snapshot(peer_dir, ["tests-pass", "lint-clean"])


def _make_repo_with_passing_test(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n"
    )
    (repo / ".peers").mkdir()
    return repo


def test_ensure_snapshot_creates_baseline_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from peers.regression_baseline import ensure_baseline_snapshot
    repo = _make_repo_with_passing_test(tmp_path)
    peer_dir = repo / ".peers"
    baseline = peer_dir / "passing-baseline.txt"

    msg = ensure_baseline_snapshot(repo, peer_dir, ["no-prior-regression"])
    assert msg is not None
    assert baseline.is_file()
    # the baseline records the passing test
    assert "test_ok" in baseline.read_text()

    # second call: baseline already present → no-op, returns None, file
    # left untouched (so a real run does not re-snapshot a regressed tree)
    before = baseline.read_text()
    msg2 = ensure_baseline_snapshot(repo, peer_dir, ["no-prior-regression"])
    assert msg2 is None
    assert baseline.read_text() == before


def test_ensure_snapshot_noop_without_gate(tmp_path: Path) -> None:
    from peers.regression_baseline import ensure_baseline_snapshot
    repo = _make_repo_with_passing_test(tmp_path)
    peer_dir = repo / ".peers"
    assert ensure_baseline_snapshot(repo, peer_dir, ["tests-pass"]) is None
    assert not (peer_dir / "passing-baseline.txt").exists()


def test_needs_snapshot_with_empty_goal_list_edge(tmp_path: Path) -> None:
    # edge: an empty goals list (zero-config substrate boot, or a goals
    # file that loaded with no entries) must NOT decide we need a
    # snapshot — the gate is absent, so no-op.
    from peers.regression_baseline import needs_baseline_snapshot
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    assert needs_baseline_snapshot(peer_dir, []) is False


def test_needs_snapshot_treats_zero_byte_baseline_as_present_edge(
    tmp_path: Path,
) -> None:
    # edge: a zero-byte passing-baseline.txt (mid-run truncation, disk
    # full at first snapshot write) is still a "file exists" signal —
    # the gate decides not to re-snapshot. This pins the boundary so a
    # later "consider zero-byte == missing" change is a deliberate
    # decision, not an accident.
    from peers.regression_baseline import needs_baseline_snapshot
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "passing-baseline.txt").write_bytes(b"")
    assert needs_baseline_snapshot(peer_dir, ["no-prior-regression"]) is False


def test_run_check_forwards_extra_args(tmp_path: Path) -> None:
    """cmd_run_check must forward check_args to the resolved script."""
    from peers.cli import cmd_run_check
    repo = _make_repo_with_passing_test(tmp_path)
    # --snapshot reaching no_regression is what creates the baseline file;
    # without arg-forwarding the check takes its compare path and never
    # writes the file.
    rc = cmd_run_check(repo, "no_regression", ("--snapshot",))
    assert rc == 0
    assert (repo / ".peers" / "passing-baseline.txt").is_file()
