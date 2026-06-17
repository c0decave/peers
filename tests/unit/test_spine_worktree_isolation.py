import subprocess
import tempfile
from pathlib import Path

import pytest
from tests.unit._isolation_helpers import _git, _attested_repo

from peers.spine.worktree import GitWorktreeProvider, prune_stale_run_worktrees
from peers.spine.gates import resolves_to_commit
from peers.spine.authorship import resolve_author


def _worktree_paths(repo):
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                         capture_output=True, text=True).stdout
    return [ln[len("worktree "):].strip() for ln in out.splitlines()
            if ln.startswith("worktree ")]


def test_lease_creates_isolated_worktree_on_named_branch(tmp_path):
    sha = _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        assert ws.branch == "peers/run/r1"
        assert ws.worktree_path.exists() and ws.worktree_path != tmp_path
        # the worktree root lives OUTSIDE the repo (placement decision) -- not
        # nested under the gitignored .peers/ runtime dir.
        assert tmp_path not in ws.worktree_path.parents
        # the worktree is checked out on its OWN named branch
        head = _git(ws.worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        assert head == "peers/run/r1"
        # shared object DB: the pre-lease attestation re-derives from the worktree
        assert resolves_to_commit(ws.worktree_path, sha) is True
        assert resolve_author(ws.worktree_path, sha) == "claude"
        # a fresh per-worktree .peers/ with its OWN run.lock exists ...
        assert (ws.worktree_path / ".peers" / "run.lock").exists()
        # ... but NO ledger: run.jsonl is never mirrored; drive() creates it fresh.
        assert not (ws.worktree_path / ".peers" / "run.jsonl").exists()


def test_lease_seeds_mirror_artifacts(tmp_path):
    _attested_repo(tmp_path)
    (tmp_path / ".peers").mkdir(exist_ok=True)
    (tmp_path / ".peers" / "passing-baseline.txt").write_text("BASE\n")  # gitignored runtime state
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        # 'git worktree add' would NOT check this out (untracked) -> provisioner mirrors it
        assert (ws.worktree_path / ".peers" / "passing-baseline.txt").read_text() == "BASE\n"


def test_release_leaves_no_leftover_worktree(tmp_path):
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        wt = ws.worktree_path
        assert str(wt) in _worktree_paths(tmp_path)
    assert str(wt) not in _worktree_paths(tmp_path)     # removed on exit
    assert not wt.exists()


def test_same_mode_run_second_lease_is_refused(tmp_path):
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1"):
        with pytest.raises(RuntimeError):               # per-worktree run.lock held
            with prov.lease(tmp_path, "r1"):
                pass


def test_two_distinct_runs_isolate_without_clobber(tmp_path):
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as a, prov.lease(tmp_path, "r2") as b:
        assert a.worktree_path != b.worktree_path
        assert a.branch != b.branch
        # each writes its OWN file on its OWN branch -- no shared HEAD
        (a.worktree_path / "only_a.txt").write_text("a")
        (b.worktree_path / "only_b.txt").write_text("b")
        assert not (b.worktree_path / "only_a.txt").exists()
        assert not (a.worktree_path / "only_b.txt").exists()


def test_stale_leaked_worktree_is_reclaimed_at_next_lease(tmp_path):
    # Simulate a HARD crash: register a peers/run/* worktree by hand and leave
    # NO flock on its run.lock (the `finally` never ran). The deterministic namer
    # makes mode_run STABLE, so without a startup sweep this would brick all
    # future r1 leases. lease() must prune the stale worktree at acquire-time.
    _attested_repo(tmp_path)
    stale = tmp_path.parent / "peers-run-stale" / "r1"
    stale.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(tmp_path), "worktree", "add", "-b",
                    "peers/run/r1", str(stale), "HEAD"],
                   capture_output=True, text=True, check=True)
    (stale / ".peers").mkdir(exist_ok=True)
    (stale / ".peers" / "run.lock").write_text("99999")   # left behind, NOT flock-held
    assert str(stale) in _worktree_paths(tmp_path)         # leaked, registered
    prov = GitWorktreeProvider()
    # the next lease of the SAME mode_run reclaims the stale leftover and succeeds
    with prov.lease(tmp_path, "r1") as ws:
        assert ws.branch == "peers/run/r1"
        assert ws.worktree_path.exists()
    assert str(stale) not in _worktree_paths(tmp_path)     # stale one swept


def test_clean_release_then_re_lease_same_mode_run(tmp_path):
    # REVIEW-C: a CLEAN lease/release must not leave the run branch behind. The
    # deterministic namer makes mode_run STABLE; if teardown removes the worktree
    # but not the branch, the next `worktree add -b peers/run/r1` fails "a branch
    # named 'peers/run/r1' already exists" -- a stable run bricked after its first
    # clean completion. (prune_stale_run_worktrees cannot reclaim it: the worktree
    # is already gone from `worktree list`, so only happy-path teardown can.)
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        assert ws.branch == "peers/run/r1"
    # after a clean release the run branch must be GONE (else re-lease bricks)
    left = _git(tmp_path, "branch", "--list", "peers/run/r1").strip()
    assert left == "", f"orphan run branch left behind after clean release: {left!r}"
    # and re-leasing the SAME mode_run succeeds
    with prov.lease(tmp_path, "r1") as ws2:
        assert ws2.branch == "peers/run/r1"
        assert ws2.worktree_path.exists()


def test_lease_temp_root_not_leaked_when_acquire_lock_poisoned(tmp_path, monkeypatch):
    # REVIEW-D: if the acquire-lock open fails (a symlink-poisoned run-locks entry
    # -> open_text_no_symlink raises OSError via O_NOFOLLOW) AFTER mkdtemp created
    # the worktree root, that root must be removed -- not leaked in /tmp.
    import peers.spine.worktree as wt_mod
    created: list[str] = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(wt_mod.tempfile, "mkdtemp",
                        lambda *a, **k: (created.append(real(*a, **k)), created[-1])[1])
    _attested_repo(tmp_path)
    locks = tmp_path / ".peers" / "run-locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "r1.lock").symlink_to(tmp_path / "decoy-target")     # poison: a symlink
    prov = GitWorktreeProvider()
    with pytest.raises(OSError):
        with prov.lease(tmp_path, "r1"):
            pass
    assert created, "mkdtemp should have been called before the failure"
    assert not any(Path(p).exists() for p in created), "leaked temp worktree root(s)"


def test_lease_worktree_add_failure_releases_lock_for_re_lease(tmp_path):
    # REVIEW-F: when `git worktree add` fails (rc!=0 -> RuntimeError), the shared
    # `finally` must still release the acquire-lock (and reclaim the conflicting
    # branch) so the failure does NOT brick a subsequent lease of the same mode_run.
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "peers/run/r1")          # pre-existing -> `worktree add -b` fails
    prov = GitWorktreeProvider()
    with pytest.raises(RuntimeError):
        with prov.lease(tmp_path, "r1"):
            pass
    # acquire-lock released + conflicting branch reclaimed -> re-lease now succeeds
    with prov.lease(tmp_path, "r1") as ws:
        assert ws.branch == "peers/run/r1"
        assert ws.worktree_path.exists()


def test_prune_skips_a_live_flock_held_worktree(tmp_path):
    # defense in depth: prune must NOT reap a worktree whose run.lock is currently
    # flock-HELD (a live run) -- only crash leftovers (unheld locks).
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as ws:
        before = _worktree_paths(tmp_path)
        prune_stale_run_worktrees(tmp_path)               # sweep while r1 is live
        assert str(ws.worktree_path) in _worktree_paths(tmp_path)  # live run untouched
        assert before == _worktree_paths(tmp_path)


# ----- full-depth-analysis #2: _git never raises TimeoutExpired out of teardown
def test_git_swallows_timeout_to_rc124(monkeypatch):
    # _git must NOT propagate subprocess.TimeoutExpired (it is a SubprocessError,
    # not suppressed by check=False) — it returns an rc=124 result so the lease
    # teardown / prune `finally` never raises (full-depth-analysis §2).
    import peers.spine.worktree as wt

    def _boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(wt.subprocess, "run", _boom)
    res = wt._git(Path("/x"), "status")
    assert res.returncode == 124 and res.stdout == ""


def test_lease_teardown_survives_git_timeout_and_releases_lock(tmp_path, monkeypatch):
    # sad: the teardown `worktree remove` times out — the lease must NOT raise and
    # must STILL release the stable acquire-lock, so the same mode_run is not bricked.
    _attested_repo(tmp_path)
    import peers.spine.worktree as wt
    orig = wt.subprocess.run
    state = {"fail_remove": True}

    def fake(argv, **kw):
        if state["fail_remove"] and "remove" in argv:
            raise subprocess.TimeoutExpired(argv, 120)
        return orig(argv, **kw)

    monkeypatch.setattr(wt.subprocess, "run", fake)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1"):       # teardown's remove will time out
        pass                               # <- must not raise out of the `with`
    state["fail_remove"] = False           # stop injecting -> allow a clean re-lease
    with prov.lease(tmp_path, "r1") as ws2:   # acquire-lock was released, not leaked
        assert ws2.mode_run == "r1"


# ----- full-depth-analysis #3: prune is serialized by the repo-wide prune-lock
def test_prune_is_gated_by_the_prune_lock(tmp_path, monkeypatch):
    # A direct prune (e.g. the Stage-7 conductor) must not run while the repo-wide
    # prune-lock is held by a lease in its add->flock window — it no-ops instead of
    # reaping a worktree mid-setup (full-depth-analysis §3).
    import fcntl
    import peers.spine.worktree as wt
    _attested_repo(tmp_path)
    # a stale leaked peers/run/* worktree prune WOULD reap (no run.lock held)
    leaked = tmp_path / "leaked"
    _git(tmp_path, "worktree", "add", "-b", "peers/run/x", str(leaked), "HEAD")

    real = wt._flock_exclusive_blocking
    monkeypatch.setattr(wt, "_flock_exclusive_blocking",
                        lambda fp, **kw: real(fp, timeout_s=0.3, interval_s=0.02))

    held = wt.open_text_no_symlink(wt._prune_lock_path(tmp_path), "a")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)           # simulate a lease mid-setup
    try:
        assert prune_stale_run_worktrees(tmp_path) == 0  # gated -> no-op
        assert leaked.exists()                           # NOT reaped mid-setup
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert prune_stale_run_worktrees(tmp_path) >= 1      # lock free -> reaps now
    assert not leaked.exists()
