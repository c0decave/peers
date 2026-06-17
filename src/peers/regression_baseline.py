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

import hashlib
from pathlib import Path
from typing import Iterable, Mapping

from peers.safe_io import read_bytes_no_symlink

_BASELINE_NAME = "passing-baseline.txt"
_GATE = "no-prior-regression"

_SKIP_BASELINE_NAME = "skip-baseline.txt"
_SKIP_GATE = "no-skipped-tests"

#: Which baseline file backs which HARD gate. The digest anchor (FU-1) keys
#: on the gate id so the convergence path can fail the *specific* gate whose
#: baseline was tampered with.
_DIGEST_FILES = {
    _GATE: _BASELINE_NAME,
    _SKIP_GATE: _SKIP_BASELINE_NAME,
}


def _baseline_digest(path: Path) -> str | None:
    """SHA-256 hex of a baseline file's bytes, or ``None`` if it is absent.

    Uses the no-symlink read so a baseline replaced by a symlink to an
    attacker-controlled file does not produce an attacker-chosen digest — the
    read raises ``OSError`` (ELOOP) which we surface as ``None`` (i.e. a
    tamper signal: it will not match a real run-start digest).
    """
    try:
        data = read_bytes_no_symlink(path)
    except (FileNotFoundError, OSError):
        return None
    return hashlib.sha256(data).hexdigest()


def snapshot_baseline_digests(peer_dir: Path) -> dict[str, str]:
    """Capture the run-start SHA-256 digest of each baseline file present.

    Returns ``{gate_id: sha256hex}`` for every baseline file that exists in
    ``peer_dir`` at the moment of the call. The orchestrator calls this ONCE
    at run start (after seeding the baselines, before any peer modifies code)
    and holds the result in process memory.

    FU-1 (P0 review 2026-06-12): the baseline files are agent-writable, so a
    peer can forge them to defeat the no-prior-regression / no-skipped-tests
    HARD gates. This anchor lives in the orchestrator's memory — which a
    separate, short-lived agent subprocess cannot reach — so it is the one
    copy of the run-start state the agent cannot forge. The convergence path
    re-derives the live digest and compares (see ``verify_baseline_digests``).
    """
    out: dict[str, str] = {}
    for gate, name in _DIGEST_FILES.items():
        digest = _baseline_digest(peer_dir / name)
        if digest is not None:
            out[gate] = digest
    return out


def verify_baseline_digests(
    peer_dir: Path, anchored: Mapping[str, str],
) -> list[str]:
    """Return the gate ids whose baseline no longer matches its run-start digest.

    The run-start baseline is a one-shot snapshot taken before any peer
    touches code and is NEVER legitimately rewritten during a run, so ANY
    divergence — a forged edit, a deletion, or a symlink swap — is a tamper
    signal. The convergence decision fails the returned gates closed.

    A gate not present in ``anchored`` is not checked (no baseline was
    snapshotted for it; the gate's own missing-baseline fail-closed path
    covers the never-snapshotted case). An empty ``anchored`` (no baseline
    gate configured) therefore yields ``[]`` — non-baseline runs are
    unaffected.
    """
    forged: list[str] = []
    for gate, expected in anchored.items():
        name = _DIGEST_FILES.get(gate)
        if name is None:
            continue
        if _baseline_digest(peer_dir / name) != expected:
            forged.append(gate)
    return sorted(forged)


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
    skips, sticking every affected band). NEW skips added after the snapshot
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
