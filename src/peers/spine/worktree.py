"""STEP-1..7 — per-run worktree / branch isolation port (Stage 5).

The isolation primitive is reached through an INJECTED port — a
runtime-checkable :class:`WorktreeProvider` Protocol the lease/propagate seam
depends on — exactly as Stage-0 injected the test-runner and Stage-1/2 injected
their adapters. This keeps the orchestration (lease-before-drive,
teardown-after, propagate-only-if-CONVERGED) deterministically testable with a
dir-copy ``FakeWorktreeProvider`` AND, where attestation + gate re-derivation
must run end-to-end, with the real :class:`GitWorktreeProvider` over a real
``tmp_path`` git repo.

This module holds:
- the port + dataclasses (:class:`RunWorkspace`, :class:`PropagationResult`,
  :class:`WorktreeProvider`) and the pure deterministic per-run namer
  :func:`workspace_names` (Task 1),
- the real :class:`GitWorktreeProvider` adapter + :func:`prune_stale_run_worktrees`
  crash-sweep (Task 2),
- :func:`propagatable_artifacts` — the Stage-7 declared-propagatable surface
  (Task 7).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from peers.safe_io import (
    _validate_single_path_component,
    atomic_write_text_in_dir_no_symlink,
    open_text_no_symlink,
)
from peers.state_store import release_run_lock

# The static .peers/ gate-inputs a leased worktree must inherit (a fresh
# `git worktree add` checks out ONLY tracked files; .peers/ is gitignored).
# EXACTLY the AsyncGateRunner._MIRROR values — kept local to keep this module
# import-light. Deliberately EXCLUDES run.lock / run.jsonl: the worktree gets
# its OWN freshly-flock'd run.lock, and its own drive() creates run.jsonl.
_MIRROR = (
    "checks",
    "checks.sha256",
    "passing-baseline.txt",
    "api-baseline.txt",
    "goals.yaml",
)
_WORKTREE_PREFIX = "peers-run-"
_RUN_BRANCH_PREFIX = "peers/run/"
#: The host-discoverable spine-runs registry (Wave-2 §5.3). Lives under the
#: REPO's `.peers/` (NOT the run-local worktree's) so a host TUI can enumerate
#: active spine mode-runs and locate each run's `run.jsonl`. OBSERVABILITY-ONLY:
#: every write/remove/prune of this registry is fail-CLOSED (swallowed) and must
#: NEVER affect lease/run correctness. Analogous to `.peers/run-locks/`.
_SPINE_RUNS_DIR = "spine-runs"
#: Grace window (seconds) below which a spine-runs record is too young to reap.
#: A just-born record an in-flight sweep races with (its acquire-lock not yet
#: flock-held) must never be collected; defense-in-depth alongside the fresh
#: at-reap-time liveness re-check.
_SPINE_RUN_REAP_GRACE_S = 10.0


@dataclass
class RunWorkspace:
    """A leased, isolated workspace for one mode-run: an own git worktree
    checked out on its own branch, with an own ``.peers/`` fault domain."""

    worktree_path: Path
    branch: str
    base_sha: str
    mode_run: str


@dataclass
class PropagationResult:
    """The outcome of an explicit, CONVERGED-gated propagation. ``ok`` is the
    only required field; ``witness`` records WHICH converged tip/artifact was
    transferred (the Stage-7 fleet-ledger edge), ``reason`` carries the
    fail-closed cause when ``ok`` is False, ``artifact`` names what moved."""

    ok: bool
    witness: dict | None = None
    reason: str = ""
    artifact: str | None = None


@runtime_checkable
class WorktreeProvider(Protocol):
    """The injected isolation port. ``lease`` is a context manager yielding a
    :class:`RunWorkspace` for the duration of a run; ``propagate`` publishes a
    converged artifact between two workspaces."""

    def lease(self, repo, mode_run, *, base=None):
        """Context manager yielding a :class:`RunWorkspace`."""
        ...

    def propagate(self, from_ws, to_ws, artifact) -> PropagationResult:
        """Publish ``artifact`` from ``from_ws`` to ``to_ws``."""
        ...


def workspace_names(base_root: Path, mode_run: str) -> tuple[Path, str]:
    """Deterministic, PURE per-run namer (touches no filesystem).

    ``branch == f"peers/run/{mode_run}"`` and ``worktree_path == base_root /
    mode_run`` — both pure functions of ``(base_root, mode_run)`` so the Stage-7
    validator can prove two runs non-colliding by name alone. ``base_root`` is
    the run-local worktree root supplied OUTSIDE the repo by ``lease``'s
    ``mkdtemp`` (the placement decision — no self-nesting under ``.peers/``).

    Fail-closed on a ``mode_run`` that is not a single safe path component:
    rejects ``/``, ``..``, ``.``, empty, any component containing ``os.sep``,
    AND a backslash ``\\`` (``_validate_single_path_component`` ACCEPTS ``a\\b``
    on Linux, but a backslash is an odd ref-name char and not the clean single
    segment the Stage-7 validator assumes — so reject it explicitly).
    """
    # reuse the shared single-component validator (rejects "", ".", "..", and
    # any name whose Path().name != name, i.e. anything with os.sep).
    _validate_single_path_component(mode_run, "mode_run")
    if mode_run in (".", "..") or os.sep in mode_run or "\\" in mode_run:
        raise ValueError(f"mode_run must be a single safe path component: {mode_run!r}")
    return (Path(base_root) / mode_run, f"{_RUN_BRANCH_PREFIX}{mode_run}")


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    # `check=False` suppresses only CalledProcessError, NOT subprocess.TimeoutExpired
    # (a SubprocessError raised regardless of `check`). The lease teardown `finally`
    # and `prune_stale_run_worktrees` BOTH document "never raises" — but a git op that
    # hangs >120s (index.lock contention from a concurrent lease, a stuck
    # `worktree remove`, an FS stall) would raise out of the finally and SKIP the
    # acquire-lock release, permanently bricking the stable mode_run on its leaked
    # lock (full-depth-analysis §2). Swallow the timeout to an rc=124 result so
    # `_git` truly never raises; rc-checking callers treat 124 as a clean failure.
    argv = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=120, check=check)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "git timed out")


def _release_lock_file(lock: Path) -> None:
    """Symlink-refusing, flock-guarded unlink of an arbitrary lock file —
    :func:`peers.state_store.release_run_lock` generalised off the hard-coded
    ``run.lock`` basename (used for the stable per-mode_run acquire-lock, which
    is NOT named ``run.lock``). Only unlinks if it can re-flock (so a live lock
    held by another holder is left alone)."""
    try:
        if lock.is_symlink():
            return
        with open_text_no_symlink(lock, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                lock.unlink()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except (FileNotFoundError, OSError):
        return


def _prune_lock_path(repo: Path) -> Path:
    lock_dir = repo / ".peers" / "run-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / ".prune.lock"


def _flock_exclusive_blocking(fp, *, timeout_s: float = 60.0,
                              interval_s: float = 0.05) -> None:
    """LOCK_EX with a bounded retry (never blocks forever — a wedged holder must
    not hang a lease). Raises ``RuntimeError`` on timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("prune-lock acquisition timed out")
            time.sleep(interval_s)


def _unflock_close(fp) -> None:
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fp.close()
    except OSError:
        pass


def _write_spine_run_record(repo: Path, mode_run: str, ws: RunWorkspace) -> None:
    """Write the host-discoverable spine-runs registry record for ``ws``.

    Wave-2 §5.3 — writes ``<repo>/.peers/spine-runs/<mode_run>.json`` with EXACTLY
    the fields the TUI reader (``peers_ctl.tui.reader.spine_runs`` /
    :class:`SpineRunEntry`) consumes: ``mode_run``, ``worktree_path`` (abs),
    ``branch``, ``ledger_path`` (= ``<worktree>/.peers/run.jsonl`` — where
    ``drive()`` writes it), ``pid`` (``os.getpid()``), ``started_at`` (ISO-8601
    UTC). The ``spine-runs/`` dir is created if absent (mirroring the
    ``run-locks/`` dir discipline) and the record is written via the no-symlink
    atomic primitive.

    OBSERVABILITY-ONLY: this is wrapped fail-CLOSED at the call site so ANY error
    here (mkdir/write) is swallowed and never reaches the lease/run. ``mode_run``
    is already validated to a single safe path component by ``workspace_names``,
    so the filename cannot traverse; it is re-validated defensively here too."""
    _validate_single_path_component(mode_run, "mode_run")
    reg_dir = Path(repo) / ".peers" / _SPINE_RUNS_DIR
    reg_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "mode_run": ws.mode_run,
        "worktree_path": str(ws.worktree_path),
        "branch": ws.branch,
        "ledger_path": str(ws.worktree_path / ".peers" / "run.jsonl"),
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text_in_dir_no_symlink(
        reg_dir / f"{mode_run}.json", json.dumps(record))


def _remove_spine_run_record(repo: Path, mode_run: str) -> None:
    """Remove the spine-runs registry record for ``mode_run`` (best-effort).

    Called from the lease teardown ``finally``. ``mode_run`` is re-validated so a
    traversal name can never unlink outside the registry dir. Wrapped fail-soft at
    the call site (OBSERVABILITY-ONLY)."""
    _validate_single_path_component(mode_run, "mode_run")
    rec = Path(repo) / ".peers" / _SPINE_RUNS_DIR / f"{mode_run}.json"
    if rec.is_symlink():
        return                                          # never follow a swapped link
    rec.unlink(missing_ok=True)


def _spine_run_acquire_lock_is_held(repo: Path, stem: str) -> bool | None:
    """Tri-state fresh-at-reap-time liveness probe for a spine-runs ``<stem>``.

    Re-derives liveness OFF THE STABLE PER-mode_run ACQUIRE-LOCK at
    ``<repo>/.peers/run-locks/<stem>.lock`` — the lock ``lease()`` flock-holds for
    the WHOLE lease (step 2-3), so a run that became live in the snapshot->reap gap
    is detectable here even though it was absent from the (stale) live snapshot.

    Returns ``True`` (held -> live -> keep), ``False`` (genuinely free/absent ->
    crash leftover -> safe to reap), or ``None`` when the lock state cannot be
    determined (a swapped symlink, an unexpected OSError) — the caller treats
    ``None`` as "do NOT reap" (conservative, matching the rc!=0 fail-CLOSED choice).
    ``stem`` is validated to a single safe path component so the lock path cannot
    traverse out of ``run-locks/``."""
    try:
        _validate_single_path_component(stem, "mode_run")
    except (ValueError, TypeError):
        return None                                     # unparseable -> don't reap
    lock = Path(repo) / ".peers" / "run-locks" / f"{stem}.lock"
    try:
        if lock.is_symlink():
            return None                                 # swapped link -> don't reap
        if not lock.exists():
            return False                                # no acquire-lock -> leftover
    except OSError:
        return None                                     # cannot tell -> don't reap
    try:
        with open_text_no_symlink(lock, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True                             # held by a live run
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False                                # free -> crash leftover
    except OSError:
        return None                                     # cannot tell -> don't reap


def _spine_record_within_grace(rec: Path) -> bool:
    """True iff ``rec`` is younger than the reap grace window by its file mtime
    (fail-soft: an unreadable/garbage mtime is treated as YOUNG -> don't reap)."""
    try:
        age = time.time() - rec.stat().st_mtime
    except (OSError, ValueError, OverflowError):
        return True                                     # can't tell age -> young
    return age < _SPINE_RUN_REAP_GRACE_S


def _prune_stale_spine_run_records(repo: Path, live_mode_runs: set[str]) -> None:
    """Reap crash-leaked spine-runs registry records (best-effort, fail-soft).

    A record is a crash leftover iff its run is NOT live. ``live_mode_runs`` is the
    START-of-sweep snapshot of every worktree whose ``.peers/run.lock`` is
    flock-HELD, computed by :func:`_prune_unlocked`. But that snapshot is
    taken BEFORE several git subprocess calls, while this glob/unlink runs AFTER —
    so a concurrent lease that becomes live IN THAT GAP is ABSENT from the snapshot
    yet must NOT be reaped (CRITICAL TOCTOU). Two independent guards (defense in
    depth) close the gap, both fail-SOFT toward keeping a record:

    1. **Fresh at-reap-time liveness re-check** — immediately before unlinking a
       candidate, re-derive liveness off the stable acquire-lock
       (:func:`_spine_run_acquire_lock_is_held`); keep if it is held OR if the lock
       state is indeterminate (``None``). This sees a run that went live in the gap.
    2. **Grace window** — additionally keep any record younger than
       ``_SPINE_RUN_REAP_GRACE_S`` by mtime, so a just-born record an in-flight
       sweep races with (acquire-lock not yet flock-held) is never collected.

    A genuinely-crashed run (snapshot-absent, acquire-lock free/absent, and past
    grace) is reaped so its host-TUI record does not linger. OBSERVABILITY-ONLY:
    never raises."""
    reg_dir = Path(repo) / ".peers" / _SPINE_RUNS_DIR
    try:
        if not reg_dir.is_dir():
            return
        records = sorted(reg_dir.glob("*.json"))
    except OSError:
        return
    for rec in records:
        try:
            if rec.is_symlink():
                continue                                # never touch a swapped link
            if rec.stem in live_mode_runs:
                continue                                # snapshot says live -> keep
            # GUARD 2: too-young -> a just-born record; never reap (defense in depth).
            if _spine_record_within_grace(rec):
                continue
            # GUARD 1: re-derive liveness FRESH at reap time off the acquire-lock.
            # Keep on held (True) AND on indeterminate (None) -> reap only when the
            # lock is genuinely free/absent (False) — closes the snapshot/glob gap.
            if _spine_run_acquire_lock_is_held(repo, rec.stem) is not False:
                continue
            rec.unlink(missing_ok=True)
        except OSError:
            continue                                    # best-effort, fail-soft


class GitWorktreeProvider:
    """The real :class:`WorktreeProvider` adapter: named-branch ``git worktree
    add`` at a ``tempfile.mkdtemp(prefix="peers-run-")`` root OUTSIDE the repo
    (no self-nesting under ``.peers/``), a stable per-``mode_run`` acquire-lock
    BEFORE the add, a per-worktree ``.peers/run.lock`` flock, an EXACTLY-``_MIRROR``
    seed (never ``run.lock``/``run.jsonl``), prune-at-acquire crash self-heal,
    and fail-closed ``worktree remove``/``prune`` teardown — generalising the
    proven :class:`peers.async_gate_runner.AsyncGateRunner` lifecycle.

    ``peers_dir`` overrides the parent ``.peers/`` to mirror from (default
    ``<repo>/.peers``).
    """

    def __init__(self, peers_dir: Path | None = None) -> None:
        self.peers_dir = Path(peers_dir) if peers_dir is not None else None

    @contextmanager
    def lease(self, repo, mode_run, *, base=None):
        repo = Path(repo)
        # 0. repo-wide PRUNE-LOCK (full-depth-analysis §3): the crash-sweep at step 1
        #    is GLOBAL, but the per-mode_run acquire-lock only serializes the SAME
        #    mode_run. A concurrent lease of a DIFFERENT mode_run (or auto_merge's
        #    `recheck-*` lease, or the Stage-7 conductor calling prune directly) could
        #    reap THIS lease's worktree in the TOCTOU window between `worktree add`
        #    (worktree becomes listable) and the per-worktree run.lock flock. Hold a
        #    repo-wide lock across prune + add + flock so no prune runs while another
        #    lease is mid-setup; release it the instant our run.lock guards us.
        prune_fp = open_text_no_symlink(_prune_lock_path(repo), "a")
        _flock_exclusive_blocking(prune_fp)
        prune_held = True
        try:
            # 1. prune-at-acquire (crash self-heal), serialized by the prune-lock we
            #    already hold (call the UNLOCKED core to avoid a same-process re-flock).
            _prune_unlocked(repo)

            # 2-3. names + the STABLE per-mode_run acquire-lock BEFORE `worktree add`.
            #    ANY failure here closes the acquire fp + removes the mkdtemp root,
            #    leaving NOTHING behind (the prune-lock is released by the outer finally).
            base_root = Path(tempfile.mkdtemp(prefix=_WORKTREE_PREFIX))
            acquire_fp = None
            try:
                wt, branch = workspace_names(base_root, mode_run)
                parent_peers = self.peers_dir or (repo / ".peers")
                lock_dir = repo / ".peers" / "run-locks"
                acquire_lock = lock_dir / f"{mode_run}.lock"
                lock_dir.mkdir(parents=True, exist_ok=True)
                acquire_fp = open_text_no_symlink(acquire_lock, "a")
                try:
                    fcntl.flock(acquire_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(f"run lock held: {mode_run}")
            except BaseException:
                if acquire_fp is not None:
                    acquire_fp.close()
                shutil.rmtree(base_root, ignore_errors=True)
                raise

            wt_lock_fp = None
            try:
                # 4. named-branch worktree add (fail-closed on rc!=0).
                add = _git(repo, "worktree", "add", "-b", branch, str(wt),
                           base or "HEAD")
                if add.returncode != 0:
                    raise RuntimeError(
                        f"worktree add failed for {mode_run}: {add.stderr.strip()}")
                base_sha = _git(repo, "rev-parse", base or "HEAD").stdout.strip()

                # 5. seed a fresh per-worktree .peers/ with its OWN flock'd run.lock.
                wt_peers = wt / ".peers"
                wt_peers.mkdir(parents=True, exist_ok=True)
                wt_lock_fp = open_text_no_symlink(wt_peers / "run.lock", "a")
                try:
                    fcntl.flock(wt_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError(f"worktree run.lock held: {mode_run}")
                wt_lock_fp.write(str(os.getpid()))
                wt_lock_fp.flush()

                # run.lock now guards this worktree from any prune -> release the
                # repo-wide prune-lock so other leases proceed during our run.
                _unflock_close(prune_fp)
                prune_held = False

                # 6. mirror EXACTLY the static _MIRROR gate-inputs (skip absent),
                #    mirroring AsyncGateRunner._mirror_peers_dir — never the whole
                #    .peers/ (would clobber the just-flock'd run.lock + import the
                #    parent's run.jsonl).
                for name in _MIRROR:
                    src = parent_peers / name
                    if not src.exists():
                        continue
                    dst = wt_peers / name
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)

                # 7. host-discoverable spine-runs registry (Wave-2 §5.3). Written
                #    AFTER the worktree+branch+run.lock are established (so the record
                #    points at a real, locked worktree). OBSERVABILITY-ONLY: wrapped
                #    fail-CLOSED — ANY error (mkdir/write) is swallowed so a registry
                #    failure makes the run merely not-host-discoverable, NEVER alters
                #    the lease result / worktree / run / return value.
                ws = RunWorkspace(worktree_path=wt, branch=branch,
                                  base_sha=base_sha, mode_run=mode_run)
                try:
                    _write_spine_run_record(repo, mode_run, ws)
                except Exception:
                    pass                                # observability-only (interrupts propagate)

                # 8. hand the leased workspace to the run.
                yield ws
            finally:
                # 9a. remove the spine-runs registry record (Wave-2 §5.3) —
                #     OBSERVABILITY-ONLY, wrapped fail-soft so a removal error never
                #     propagates out of teardown or skips the real worktree/branch/lock
                #     cleanup below. A failed remove is reaped later by
                #     prune_stale_run_worktrees (the crash-sweep).
                try:
                    _remove_spine_run_record(repo, mode_run)
                except Exception:
                    pass                                # observability-only (interrupts propagate)

                # 9b. teardown — order matters: CLOSE each lock fp BEFORE the
                #    re-flocking unlink (an open held fd would make the unlink see
                #    the lock busy and skip it). Everything check=False (and `_git`
                #    never raises, full-depth-analysis §2) — never raise out of the
                #    finally, so the acquire-lock below is ALWAYS released.
                if wt_lock_fp is not None:
                    try:
                        fcntl.flock(wt_lock_fp.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                    wt_lock_fp.close()
                    release_run_lock(wt / ".peers")
                _git(repo, "worktree", "remove", "--force", str(wt))
                _git(repo, "worktree", "prune")
                # REVIEW-C: delete the run branch too — a clean `worktree remove`
                # leaves peers/run/<mode_run> behind, so the NEXT lease of the same
                # STABLE mode_run would fail "a branch already exists" (and prune
                # cannot reclaim it once the worktree is gone from `worktree list`).
                # Mirrors the crash-path branch -D in prune. check=False — never raises.
                _git(repo, "branch", "-D", branch)
                shutil.rmtree(base_root, ignore_errors=True)
                try:
                    fcntl.flock(acquire_fp.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                acquire_fp.close()
                _release_lock_file(acquire_lock)
        finally:
            # release the prune-lock on ANY early exit (prune/mkdtemp/acquire/add
            # failure) where it was not already released after step 5.
            if prune_held:
                _unflock_close(prune_fp)

    def propagate(self, from_ws, to_ws, artifact) -> PropagationResult:
        # The CONVERGED-gated edge is driven ONLY via the module-level
        # propagate_branch() in propagate.py (it needs the producer's
        # ModeRun.ledger, which a RunWorkspace lacks). The provider stays a pure
        # mechanism — never the policy.
        raise NotImplementedError(  # JUSTIFIED: deliberate wrong-entry-point guard, not a stub — the CONVERGED-gated edge is propagate_branch's job (Protocol propagate() only gets RunWorkspaces, lacks the producer ModeRun.ledger); spec Task 2/4
            "use peers.spine.propagate.propagate_branch (CONVERGED-gated)")


def prune_stale_run_worktrees(repo) -> int:
    """Remove crash-leaked ``peers/run/*`` worktrees (the happy path auto-removes
    them in ``lease``'s ``finally``; a HARD crash bypasses it). A worktree is a
    crash leftover iff its ``.peers/run.lock`` is NOT currently flock-HELD (or is
    absent); a LIVE run (flock held) is SKIPPED and its branch never touched.
    A pruned worktree leaves its branch behind, so the orphaned ``peers/run/<name>``
    branch is ``branch -D``'d too (else the next ``worktree add -b`` fails
    "branch already exists"). Returns the count pruned. Safe at startup; called
    by ``lease`` step 1 and exposed for the Stage-7 conductor. Never raises.

    ALSO reaps crash-leaked spine-runs registry records (Wave-2 §5.3) whose run is
    not live: any ``<repo>/.peers/spine-runs/<name>.json`` whose ``<name>`` is NOT
    among the live (flock-held) ``peers/run/*`` worktrees is removed, so a crashed
    run's host-discoverable record does not linger. A live run's record is kept.
    OBSERVABILITY-ONLY: the registry reap is fail-soft and never affects the
    worktree-prune count or result.

    Serialized by the repo-wide prune-lock (full-depth-analysis §3) so a direct
    caller (e.g. the conductor) cannot reap a worktree another ``lease`` is between
    ``worktree add`` and its run.lock flock. ``lease`` already holds that lock, so
    it calls :func:`_prune_unlocked` directly (a same-process re-flock would
    self-deadlock)."""
    repo = Path(repo)
    prune_fp = open_text_no_symlink(_prune_lock_path(repo), "a")
    try:
        _flock_exclusive_blocking(prune_fp)
        return _prune_unlocked(repo)
    except RuntimeError:
        return 0                                       # prune-lock timeout -> no-op
    finally:
        _unflock_close(prune_fp)


def _prune_unlocked(repo) -> int:
    """The prune core (assumes the repo-wide prune-lock is already held).

    ALSO snapshots the live (flock-held) ``peers/run/*`` mode_runs and reaps
    crash-leaked spine-runs registry records (Wave-2 §5.3) for runs that are NOT
    live — fail-soft, never affecting the worktree-prune count or result."""
    repo = Path(repo)
    out = _git(repo, "worktree", "list", "--porcelain")
    if out.returncode != 0:
        # Cannot enumerate worktrees -> we cannot tell live from stale, so leave
        # registry records untouched (fail-CLOSED: never reap a possibly-live one).
        return 0

    records: list[tuple[str, str | None]] = []
    cur_path: str | None = None
    cur_branch: str | None = None
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):].strip()
            cur_branch = None
        elif line.startswith("branch "):
            cur_branch = line[len("branch "):].strip()
        elif not line.strip():
            if cur_path is not None:
                records.append((cur_path, cur_branch))
            cur_path = None
            cur_branch = None
    if cur_path is not None:                       # flush a trailing record
        records.append((cur_path, cur_branch))

    pruned = 0
    live_mode_runs: set[str] = set()               # mode_runs whose run.lock is held
    for path, branch in records:
        if not branch or not branch.startswith(f"refs/heads/{_RUN_BRANCH_PREFIX}"):
            continue
        if _run_lock_is_held(Path(path) / ".peers" / "run.lock"):
            # live run -> never reap; remember its mode_run so its registry
            # record (Wave-2 §5.3) is KEPT by the registry reap below.
            live_mode_runs.add(branch[len(f"refs/heads/{_RUN_BRANCH_PREFIX}"):])
            continue                               # live run -> never reap
        _git(repo, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
        branch_name = branch[len("refs/heads/"):]  # peers/run/<name>
        _git(repo, "branch", "-D", branch_name)
        pruned += 1
    _git(repo, "worktree", "prune")
    # OBSERVABILITY-ONLY: reap stale registry records (kept fail-soft + isolated
    # from the worktree-prune result above). lease() calls _prune_unlocked at
    # step 1 under the prune-lock, so the registry reap also fires there.
    try:
        _prune_stale_spine_run_records(repo, live_mode_runs)
    except Exception:
        pass                                            # observability-only (interrupts propagate)
    return pruned


def propagatable_artifacts(run) -> list[str]:
    """The declared set of artifacts a run CAN propagate — its run branch (Stage-7
    surface). The [07 §7.2] validator checks a dependency's required artifact
    against this set, so "a dependency that requires an artifact the producer
    mode cannot emit" is computable later. A legacy single-HEAD run
    (``branch=None``) has no isolated artifact to propagate -> ``[]``. (Exposure
    only — the validator that USES this is out of scope for Stage 5.)"""
    return [run.branch] if run.branch is not None else []


def _run_lock_is_held(lock: Path) -> bool:
    """True iff ``lock`` exists and is currently flock-HELD by another holder
    (a live run). An absent/unreadable lock or one we can flock ourselves is a
    crash leftover (False)."""
    try:
        with open_text_no_symlink(lock, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True                        # held by a live run
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except (FileNotFoundError, OSError):
        return False                               # lock/parent absent -> leftover
