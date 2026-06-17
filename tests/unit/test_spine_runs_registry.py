"""Wave-2 §5.3 — host-discoverable spine-runs registry.

``lease()`` writes a small, fail-CLOSED, observability-only record to
``<repo>/.peers/spine-runs/<mode_run>.json`` so a host TUI can enumerate active
spine mode-runs and locate each run's ``run.jsonl``. The record is removed on
clean teardown and a crashed run's stale record is reaped by
``prune_stale_run_worktrees``. The registry is observability-only: ANY error in
the write/remove/prune of the registry must NEVER affect the lease/run/return.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from tests.unit._isolation_helpers import _attested_repo

from peers.spine.worktree import (
    GitWorktreeProvider,
    _prune_stale_spine_run_records,
    prune_stale_run_worktrees,
)


def _registry_path(repo, mode_run):
    return Path(repo) / ".peers" / "spine-runs" / f"{mode_run}.json"


def _worktree_paths(repo):
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout
    return [ln[len("worktree "):].strip() for ln in out.splitlines()
            if ln.startswith("worktree ")]


# --------------------------------------------------------------------------- #
# happy: lease writes the record (exact fields) + clean teardown removes it    #
# --------------------------------------------------------------------------- #
def test_lease_writes_registry_record_pointing_at_real_worktree(tmp_path):
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    reg = _registry_path(tmp_path, "r1")
    with prov.lease(tmp_path, "r1") as ws:
        assert reg.exists(), "registry record not written at lease-acquire"
        rec = json.loads(reg.read_text())
        # EXACTLY the fields the TUI reader (SpineRunEntry) consumes.
        assert set(rec) == {"mode_run", "worktree_path", "branch",
                            "ledger_path", "pid", "started_at"}
        assert rec["mode_run"] == "r1"
        assert rec["branch"] == ws.branch == "peers/run/r1"
        # worktree_path is absolute and points at the REAL leased worktree.
        assert rec["worktree_path"] == str(ws.worktree_path)
        assert Path(rec["worktree_path"]).is_absolute()
        assert Path(rec["worktree_path"]).exists()
        # ledger_path is <worktree>/.peers/run.jsonl (where drive() will write it).
        assert rec["ledger_path"] == str(ws.worktree_path / ".peers" / "run.jsonl")
        assert rec["pid"] == os.getpid()
        # started_at is a parseable ISO-8601 timestamp.
        from datetime import datetime
        datetime.fromisoformat(rec["started_at"])
    # clean teardown removes the record.
    assert not reg.exists(), "registry record not removed on clean teardown"


def test_lease_registry_dir_created_if_absent(tmp_path):
    _attested_repo(tmp_path)
    # the spine-runs dir does not exist yet -> lease must create it.
    assert not (tmp_path / ".peers" / "spine-runs").exists()
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1"):
        assert (tmp_path / ".peers" / "spine-runs").is_dir()


# --------------------------------------------------------------------------- #
# sad (fail-CLOSED, load-bearing): a registry write failure NEVER breaks lease #
# --------------------------------------------------------------------------- #
def test_registry_write_failure_does_not_break_lease(tmp_path, monkeypatch):
    # Make the registry write blow up. The lease MUST still succeed and return a
    # correct, fully-built RunWorkspace; the run is unaffected, just not
    # host-discoverable. (observability-only invariant.)
    import peers.spine.worktree as wt_mod
    monkeypatch.setattr(
        wt_mod, "_write_spine_run_record",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        # the lease succeeded and the workspace is fully built ...
        assert ws.branch == "peers/run/r1"
        assert ws.worktree_path.exists()
        assert (ws.worktree_path / ".peers" / "run.lock").exists()
        head = subprocess.run(
            ["git", "-C", str(ws.worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True).stdout.strip()
        assert head == "peers/run/r1"
        # ... and the run is on its own branch, isolated and writable.
        (ws.worktree_path / "only_r1.txt").write_text("ok")
        # the registry record was NOT written (the write failed, swallowed).
        assert not _registry_path(tmp_path, "r1").exists()
    # the registry record is still absent; teardown stayed clean too.
    assert not _registry_path(tmp_path, "r1").exists()


def test_registry_remove_failure_does_not_break_teardown(tmp_path, monkeypatch):
    # A failing registry REMOVAL in teardown must be swallowed: the worktree /
    # branch / locks must still be torn down normally (the run is unaffected).
    import peers.spine.worktree as wt_mod
    monkeypatch.setattr(
        wt_mod, "_remove_spine_run_record",
        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")),
    )
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        wt = ws.worktree_path
        assert str(wt) in _worktree_paths(tmp_path)
    # teardown completed despite the registry-remove failure:
    assert str(wt) not in _worktree_paths(tmp_path)
    assert not wt.exists()
    # and re-leasing the same stable mode_run still works (branch was -D'd).
    with prov.lease(tmp_path, "r1") as ws2:
        assert ws2.worktree_path.exists()


# --------------------------------------------------------------------------- #
# edge: crash-leak reaped by prune; a LIVE run's record kept; name-traversal   #
# --------------------------------------------------------------------------- #
def test_prune_reaps_stale_registry_record(tmp_path):
    # A crashed run leaves a registry record whose worktree/run.lock is gone.
    # prune_stale_run_worktrees must reap the stale registry entry too.
    _attested_repo(tmp_path)
    reg_dir = tmp_path / ".peers" / "spine-runs"
    reg_dir.mkdir(parents=True, exist_ok=True)
    stale = reg_dir / "ghost.json"
    stale.write_text(json.dumps({
        "mode_run": "ghost",
        "worktree_path": str(tmp_path.parent / "peers-run-ghost" / "ghost"),
        "branch": "peers/run/ghost",
        "ledger_path": str(tmp_path.parent / "peers-run-ghost" / "ghost"
                           / ".peers" / "run.jsonl"),
        "pid": 999999, "started_at": "2026-06-11T00:00:00+00:00",
    }))
    # age it past the reap grace window (a just-crashed run leaves an aged record;
    # the grace only protects a record born within the last few seconds).
    old = time.time() - 3600.0
    os.utime(stale, (old, old))
    assert stale.exists()
    prune_stale_run_worktrees(tmp_path)
    assert not stale.exists(), "stale registry record not reaped by prune"


def test_prune_keeps_a_live_runs_registry_record(tmp_path):
    # defense in depth: a LIVE run (its run.lock flock-HELD) must keep its
    # registry record across a prune sweep.
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1"):
        reg = _registry_path(tmp_path, "r1")
        assert reg.exists()
        prune_stale_run_worktrees(tmp_path)          # sweep while r1 is live
        assert reg.exists(), "live run's registry record was wrongly reaped"
        # AND the harder case: even if r1 were ABSENT from the live snapshot
        # (the become-live-during-sweep TOCTOU window), the fresh re-check off
        # the held acquire-lock must keep it.
        _prune_stale_spine_run_records(tmp_path, set())
        assert reg.exists(), (
            "live run's record reaped when missing from the (stale) live snapshot")


def test_registry_filename_cannot_traverse(tmp_path):
    # the namer already validates mode_run to a single safe path component, so a
    # traversal mode_run is rejected BEFORE any worktree/registry write happens.
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with pytest.raises(ValueError):
        with prov.lease(tmp_path, "../escape"):
            pass
    # nothing escaped into the parent dir as a registry file.
    assert not (tmp_path / ".peers" / "spine-runs").exists() or \
        list((tmp_path / ".peers" / "spine-runs").glob("*.json")) == []


# --------------------------------------------------------------------------- #
# CRITICAL TOCTOU: a record that becomes live IN THE SNAPSHOT->REAP GAP must   #
# survive — liveness is re-derived FRESH at reap time off the acquire-lock.    #
# --------------------------------------------------------------------------- #
def _make_aged_record(repo, mode_run, *, age_s=3600.0):
    """Write a spine-runs record and back-date its mtime so it is past the grace
    window (so grace alone never explains a survive/reap result)."""
    reg_dir = Path(repo) / ".peers" / "spine-runs"
    reg_dir.mkdir(parents=True, exist_ok=True)
    rec = reg_dir / f"{mode_run}.json"
    rec.write_text(json.dumps({
        "mode_run": mode_run,
        "worktree_path": str(Path(repo).parent / f"peers-run-{mode_run}" / mode_run),
        "branch": f"peers/run/{mode_run}",
        "ledger_path": str(Path(repo).parent / f"peers-run-{mode_run}" / mode_run
                           / ".peers" / "run.jsonl"),
        "pid": 999999,
        "started_at": "2026-01-01T00:00:00+00:00",
    }))
    old = time.time() - age_s
    os.utime(rec, (old, old))
    return rec


def test_prune_does_not_reap_a_record_that_became_live_during_the_sweep(tmp_path):
    # THE LOAD-BEARING REGRESSION. `live_mode_runs` is snapshotted at the START of
    # the sweep; a concurrent lease that becomes live IN THE GAP holds the stable
    # acquire-lock at run-locks/<stem>.lock but is ABSENT from that stale snapshot.
    # The fresh re-check at reap time must see the lock held and KEEP the record.
    _attested_repo(tmp_path)
    rec = _make_aged_record(tmp_path, "racer")          # aged -> grace can't save it
    lock_dir = tmp_path / ".peers" / "run-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / "racer.lock"
    # simulate the concurrent lease that became live in the gap: hold the stable
    # per-mode_run acquire-lock for the duration of the reap.
    with open(lock, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # "racer" is NOT in the (stale) live set passed to the reaper.
            _prune_stale_spine_run_records(tmp_path, set())
            assert rec.exists(), (
                "a record that became live during the sweep was wrongly reaped "
                "(stale snapshot + glob/unlink gap TOCTOU)")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def test_prune_reaps_a_genuinely_stale_record_no_lock_held(tmp_path):
    # The negative control: no acquire-lock held + old mtime -> genuinely crashed
    # -> the fresh re-check sees the lock free/absent and the record IS reaped.
    _attested_repo(tmp_path)
    rec = _make_aged_record(tmp_path, "ghost")
    assert not (tmp_path / ".peers" / "run-locks" / "ghost.lock").exists()
    _prune_stale_spine_run_records(tmp_path, set())
    assert not rec.exists(), "a genuinely-stale record (no lock, old) was not reaped"


# --------------------------------------------------------------------------- #
# grace window: a just-born record an in-flight sweep races with is never reaped #
# --------------------------------------------------------------------------- #
def test_prune_grace_window_keeps_a_fresh_record_even_without_a_lock(tmp_path):
    # A just-written record (fresh mtime) with NO lock held must NOT be reaped:
    # an in-flight sweep may race a still-acquiring lease whose acquire-lock isn't
    # flock-held yet. Grace by file mtime protects it.
    _attested_repo(tmp_path)
    rec = _make_aged_record(tmp_path, "newborn", age_s=0.0)  # fresh mtime
    now = time.time()
    os.utime(rec, (now, now))
    assert not (tmp_path / ".peers" / "run-locks" / "newborn.lock").exists()
    _prune_stale_spine_run_records(tmp_path, set())
    assert rec.exists(), "a fresh (within-grace) record was wrongly reaped"


def test_prune_grace_window_reaps_the_same_record_once_aged(tmp_path):
    # The same record, once aged past the grace window (and still no lock held),
    # IS reaped — grace only protects the newborn, not a real crash leftover.
    _attested_repo(tmp_path)
    rec = _make_aged_record(tmp_path, "newborn", age_s=0.0)
    now = time.time()
    os.utime(rec, (now, now))
    _prune_stale_spine_run_records(tmp_path, set())
    assert rec.exists()                                  # fresh -> grace keeps it
    old = time.time() - 3600.0                           # age it past the grace
    os.utime(rec, (old, old))
    _prune_stale_spine_run_records(tmp_path, set())
    assert not rec.exists(), "an aged, lock-free record was not reaped after grace"


def test_prune_grace_fail_soft_on_unreadable_mtime_treats_as_young(tmp_path, monkeypatch):
    # Defense in depth / fail-soft: if the mtime cannot be read (garbage/missing),
    # treat the record as YOUNG (do NOT reap) — conservative, matching the
    # "can't tell -> don't reap" posture. Stat raises -> record survives.
    _attested_repo(tmp_path)
    rec = _make_aged_record(tmp_path, "weird")           # aged + no lock => normally reaped
    real_stat = Path.stat

    def _boom_stat(self, *a, **k):
        if self == rec:
            raise OSError("cannot stat")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", _boom_stat)
    _prune_stale_spine_run_records(tmp_path, set())
    real_stat(rec)
