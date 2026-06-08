"""Auto-snapshot the no-prior-regression baseline at run start.

`no_regression` returns failure when `.peers/passing-baseline.txt` is
missing ("run once with --snapshot"), but nothing ever creates that file —
so in implement-mode the gate fails forever and the convergence-wall halt
sticks every run at `stuck:no-prior-regression` (calc v2 diagnostic,
2026-05-31). The driver calls `ensure_baseline_snapshot` ONCE at run start
(before peers modify code) to capture the baseline when the gate is
configured and no baseline exists yet. A mid-run deletion still fails closed
— the check keeps treating a missing baseline as failure; we only seed it
at the very start of a fresh run.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

_BASELINE_NAME = "passing-baseline.txt"
_GATE = "no-prior-regression"

_SKIP_BASELINE_NAME = "skip-baseline.txt"
_SKIP_GATE = "no-skipped-tests"


def needs_baseline_snapshot(peer_dir: Path, goal_ids: Iterable[str]) -> bool:
    """True iff the no-prior-regression gate is configured and no baseline
    has been captured yet."""
    if _GATE not in set(goal_ids):
        return False
    return not (peer_dir / _BASELINE_NAME).is_file()


def ensure_baseline_snapshot(
    repo: Path, peer_dir: Path, goal_ids: Iterable[str],
) -> str | None:
    """Snapshot the regression baseline once at run start.

    Returns a one-line operator message when a snapshot was taken (or
    attempted), or None when nothing was needed. `cmd_run_check` is
    imported lazily to avoid a cli<->driver import cycle.
    """
    if not needs_baseline_snapshot(peer_dir, goal_ids):
        return None
    from peers.cli import cmd_run_check

    rc = cmd_run_check(repo, "no_regression", ("--snapshot",))
    if rc == 0:
        return (
            "no-prior-regression: captured run-start baseline at "
            f"{peer_dir / _BASELINE_NAME}"
        )
    return (
        f"no-prior-regression: baseline snapshot FAILED (rc={rc}); the gate "
        "will fail until a baseline exists"
    )


def needs_skip_baseline(peer_dir: Path, goal_ids: Iterable[str]) -> bool:
    """True iff the no-skipped-tests gate is configured and no skip baseline
    has been captured yet."""
    if _SKIP_GATE not in set(goal_ids):
        return False
    return not (peer_dir / _SKIP_BASELINE_NAME).is_file()


def ensure_skip_baseline(
    repo: Path, peer_dir: Path, goal_ids: Iterable[str],
) -> str | None:
    """Snapshot the skip baseline once at run start.

    Grandfathers skips already present in ``tests/`` at run start so the
    no-skipped-tests gate doesn't permanently block a fresh implement-mode
    run on inherited / pre-baseline skips (it kept flagging all 12 inherited
    skips, sticking every coredev band). NEW skips added after the snapshot
    are still enforced. Mirrors `ensure_baseline_snapshot`; `cmd_run_check`
    is imported lazily to avoid a cli<->driver import cycle.
    """
    if not needs_skip_baseline(peer_dir, goal_ids):
        return None
    from peers.cli import cmd_run_check

    rc = cmd_run_check(repo, "no_skipped_tests", ("--snapshot",))
    if rc == 0:
        return (
            "no-skipped-tests: captured run-start skip baseline at "
            f"{peer_dir / _SKIP_BASELINE_NAME}"
        )
    return (
        f"no-skipped-tests: skip-baseline snapshot FAILED (rc={rc}); the gate "
        "may flag inherited skips until a baseline exists"
    )
