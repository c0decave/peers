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


# --- skip-baseline snapshot (no-skipped-tests gate-scoping) ---------------
#
# Mirrors the no-prior-regression baseline above: snapshot the skips present
# at run-start ONCE so inherited / pre-baseline skips are grandfathered and
# don't block a fresh implement-mode run, while NEW skips still fail.


def _make_repo_with_inherited_skip(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_skips.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip(reason='inherited')\n"
        "def test_old():\n    pass\n"
    )
    (repo / ".peers").mkdir()
    return repo


def test_ensure_skip_baseline_creates_and_is_idempotent(
    tmp_path: Path,
) -> None:
    from peers.regression_baseline import ensure_skip_baseline
    repo = _make_repo_with_inherited_skip(tmp_path)
    peer_dir = repo / ".peers"
    baseline = peer_dir / "skip-baseline.txt"

    msg = ensure_skip_baseline(repo, peer_dir, ["no-skipped-tests"])
    assert msg is not None
    assert baseline.is_file()
    assert "test_skips.py" in baseline.read_text()

    # second call: baseline already present → no-op, file untouched
    before = baseline.read_text()
    assert ensure_skip_baseline(repo, peer_dir, ["no-skipped-tests"]) is None
    assert baseline.read_text() == before


def test_ensure_skip_baseline_noop_without_gate(tmp_path: Path) -> None:
    from peers.regression_baseline import ensure_skip_baseline
    repo = _make_repo_with_inherited_skip(tmp_path)
    peer_dir = repo / ".peers"
    assert ensure_skip_baseline(repo, peer_dir, ["tests-pass"]) is None
    assert not (peer_dir / "skip-baseline.txt").exists()


def test_ensure_skip_baseline_grandfathers_inherited_skip(
    tmp_path: Path,
) -> None:
    """End-to-end: after the snapshot, the gate passes on the inherited
    skip but still fails a newly added one."""
    from peers.regression_baseline import ensure_skip_baseline
    from peers.cli import cmd_run_check
    repo = _make_repo_with_inherited_skip(tmp_path)
    peer_dir = repo / ".peers"

    assert ensure_skip_baseline(repo, peer_dir, ["no-skipped-tests"])
    # inherited skip is grandfathered → gate clean
    assert cmd_run_check(repo, "no_skipped_tests", ()) == 0
    # a brand-new unsigned skip is still a violation
    (repo / "tests" / "test_new.py").write_text(
        "import pytest\n@pytest.mark.skip\ndef test_new():\n    pass\n"
    )
    assert cmd_run_check(repo, "no_skipped_tests", ()) == 1


# --- FU-1: substrate-held baseline digest anchor (forgery resistance) ------
#
# The baseline files (.peers/passing-baseline.txt, .peers/skip-baseline.txt)
# are agent-writable. A peer that forges them defeats the no-prior-regression
# and no-skipped-tests HARD gates (P0 review, 2026-06-12). The run-start
# baseline is a one-shot snapshot taken BEFORE any peer touches code and is
# NEVER legitimately rewritten mid-run — so the orchestrator captures its
# SHA-256 digest in process memory at run start (snapshot_baseline_digests)
# and, at the convergence decision, re-derives the live digest and refuses to
# converge if it diverged (verify_baseline_digests). The anchor lives in the
# orchestrator's memory, which a separate short-lived agent subprocess cannot
# reach, so it cannot be forged the way the on-disk file can.


def _peer_dir_with_baselines(tmp_path: Path) -> Path:
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "passing-baseline.txt").write_text(
        "tests.test_calc::test_addition\ntests.test_calc::test_subtraction\n"
    )
    (peer_dir / "skip-baseline.txt").write_text(
        "tests/test_s.py|pytest.mark.skip|test_old|@pytest.mark.skip|abcdef0123456789\n"
    )
    return peer_dir


def test_snapshot_baseline_digests_captures_present_baselines(
    tmp_path: Path,
) -> None:
    # happy: both baseline files present → both gate-ids anchored with a
    # 64-hex sha256 digest, keyed by the gate id the file backs.
    from peers.regression_baseline import snapshot_baseline_digests
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    assert set(anchor) == {"no-prior-regression", "no-skipped-tests"}
    for digest in anchor.values():
        assert len(digest) == 64
        int(digest, 16)  # is hex


def test_snapshot_baseline_digests_empty_when_no_baselines(
    tmp_path: Path,
) -> None:
    # edge: no baseline files (gate not configured / never snapshotted) →
    # empty anchor; nothing to verify, the gate's own missing-baseline
    # fail-closed path handles the never-snapshotted case.
    from peers.regression_baseline import snapshot_baseline_digests
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    assert snapshot_baseline_digests(peer_dir) == {}


def test_verify_baseline_digests_clean_when_unchanged(tmp_path: Path) -> None:
    # happy: snapshot then verify with the file untouched → no forgery.
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    assert verify_baseline_digests(peer_dir, anchor) == []


def test_verify_baseline_digests_detects_forged_passing_baseline(
    tmp_path: Path,
) -> None:
    # sad/security: the PoC forge — drop a (now-red) test from the passing
    # baseline so the no-prior-regression gate would not flag the regression.
    # The digest anchor MUST catch it.
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    (peer_dir / "passing-baseline.txt").write_text(
        "tests.test_calc::test_addition\n"  # dropped test_subtraction
    )
    assert verify_baseline_digests(peer_dir, anchor) == ["no-prior-regression"]


def test_verify_baseline_digests_detects_forged_skip_baseline(
    tmp_path: Path,
) -> None:
    # sad/security: the PoC forge — grandfather a NEW skip by adding its
    # signature to the skip baseline. The digest anchor MUST catch it.
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    with (peer_dir / "skip-baseline.txt").open("a") as f:
        f.write(
            "tests/test_new.py|pytest.mark.skip|test_new|@pytest.mark.skip|0000000000000000\n"
        )
    assert verify_baseline_digests(peer_dir, anchor) == ["no-skipped-tests"]


def test_verify_baseline_digests_detects_deleted_baseline(
    tmp_path: Path,
) -> None:
    # sad/edge: deleting an anchored baseline after snapshot is also tamper
    # (a peer cannot dodge verification by removing the file) → flagged.
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    (peer_dir / "passing-baseline.txt").unlink()
    assert verify_baseline_digests(peer_dir, anchor) == ["no-prior-regression"]


def test_verify_baseline_digests_detects_both_forged(tmp_path: Path) -> None:
    # sad: forging BOTH baselines flags BOTH gates (sorted, deterministic).
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    (peer_dir / "passing-baseline.txt").write_text("only::one\n")
    (peer_dir / "skip-baseline.txt").write_text("")
    assert verify_baseline_digests(peer_dir, anchor) == [
        "no-prior-regression",
        "no-skipped-tests",
    ]


def test_verify_baseline_digests_empty_anchor_is_clean(tmp_path: Path) -> None:
    # edge: an empty anchor (no baseline gate configured at run-start) means
    # there is nothing to verify — even a baseline appearing on disk later is
    # not checked here (it was not part of the run-start snapshot).
    from peers.regression_baseline import verify_baseline_digests
    peer_dir = _peer_dir_with_baselines(tmp_path)
    assert verify_baseline_digests(peer_dir, {}) == []


def test_verify_baseline_digests_refuses_symlinked_baseline(
    tmp_path: Path,
) -> None:
    # sad/security: a peer cannot dodge the digest by replacing the baseline
    # with a symlink to an attacker-controlled file — the no-symlink read
    # raises, which the verifier treats as a tamper (digest != anchor).
    from peers.regression_baseline import (
        snapshot_baseline_digests,
        verify_baseline_digests,
    )
    peer_dir = _peer_dir_with_baselines(tmp_path)
    anchor = snapshot_baseline_digests(peer_dir)
    target = tmp_path / "evil.txt"
    target.write_text("tests.test_calc::test_addition\n")
    baseline = peer_dir / "passing-baseline.txt"
    baseline.unlink()
    baseline.symlink_to(target)
    assert "no-prior-regression" in verify_baseline_digests(peer_dir, anchor)
