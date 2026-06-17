# tests/unit/test_spine_auto_merge.py
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tests.unit._isolation_helpers import (_git, _attested_repo, _commit_on_branch,
                                          _run, _converged_branch_ledger)

from peers.spine.auto_merge import land, LandingResult
from peers.spine.gates import resolves_to_commit
from peers.spine.authorship import resolve_author


class _SameRepoProvider:
    """A WorktreeProvider stand-in whose `lease` adds a DETACHED git worktree at
    the requested `base` (the converged commit) in the SAME repo, so the injected
    recheck runs against the converged TREE -- not the live branch tip. Teardown
    removes the worktree. Mirrors GitWorktreeProvider's lease contract enough for
    land()'s recheck lease (yields a RunWorkspace with worktree_path/base_sha)."""
    def __init__(self):
        self.leased = []
    @contextmanager
    def lease(self, repo, mode_run, *, base=None):
        import tempfile
        from peers.spine.worktree import RunWorkspace
        wt = Path(tempfile.mkdtemp(prefix="recheck-"))
        _git(repo, "worktree", "add", "--detach", str(wt), base or "HEAD")
        self.leased.append((wt, base))
        try:
            yield RunWorkspace(worktree_path=wt, branch="(detached)",
                               base_sha=base or "", mode_run=mode_run)
        finally:
            _git(repo, "worktree", "remove", "--force", str(wt))
            _git(repo, "worktree", "prune")


def _producer_on_branch(tmp_path, mode_run="p1", landing="auto-merge", peer="claude"):
    """A converged develop run on a real attested branch peers/run/<mode_run>.
    `base_sha` is the repo HEAD at branch time -- the RUN's recorded fork point
    (what GitWorktreeProvider.lease captures and threads onto ModeRun)."""
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    tip = _commit_on_branch(tmp_path, f"peers/run/{mode_run}", "fix.py", "fix", peer=peer)
    run = _run(tmp_path, mode_run=mode_run, branch=f"peers/run/{mode_run}")
    run.base_sha = base                                # the run's recorded base (S4)
    run.op_config.landing = landing
    run._ledger = _converged_branch_ledger(tmp_path, tmp_path / f"{mode_run}.jsonl",
                                           mode_run, tip)
    return run, tip


def _target_sha(repo, ref="refs/heads/main"):
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def test_land_merges_converged_commit_after_passing_recheck(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")                     # the target branch (at base)
    run, tip = _producer_on_branch(tmp_path)
    prov = _SameRepoProvider()
    res = land(run, provider=prov, target_ref="main",
               recheck=lambda wt, commit: True, repo=tmp_path)
    assert isinstance(res, LandingResult)                # the documented return type
    assert res.landed is True and res.merged_sha == tip
    assert res.target_ref == "refs/heads/main"           # normalised to the canonical ref
    # the target branch now resolves to the converged commit (CAS ff-only)
    assert _target_sha(tmp_path, "refs/heads/main") == tip
    assert resolves_to_commit(tmp_path, tip) is True
    # the recheck leased a worktree AT the converged commit (the converged tree)
    assert prov.leased and prov.leased[-1][1] == tip
    # the landed row is attested to the PRODUCER's peer (no re-attest); independence
    # is derived from the attested author (REVIEW-B second layer), NOT a literal True.
    landed = [r for r in run.ledger.read() if r.event == "landed"]
    assert landed and landed[-1].author == "claude" and landed[-1].independence is True
    assert landed[-1].witness["kind"] == "git-sha" and landed[-1].witness["sha256"] == tip


def test_land_refuses_when_not_auto_merge(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    run, tip = _producer_on_branch(tmp_path, landing="branch-pr")   # operator did NOT ask
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "not-auto-merge"
    assert _target_sha(tmp_path) == before                         # target untouched


def test_land_refuses_on_bad_target_ref(tmp_path):
    # B6: a target that does not resolve to a local refs/heads/<name> branch
    # (a tag, a fully-qualified double, HEAD, a non-existent branch) -> fail-closed.
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    _git(tmp_path, "tag", "v1")                          # a TAG, not a branch
    run, tip = _producer_on_branch(tmp_path)
    for bad in ("v1", "HEAD", "refs/heads/refs/heads/main", "no-such-branch",
                "refs/remotes/origin/main"):
        before = _target_sha(tmp_path)
        res = land(run, provider=_SameRepoProvider(), target_ref=bad,
                   recheck=lambda wt, c: True, repo=tmp_path)
        assert res.landed is False and res.reason == "bad-target-ref", bad
        assert _target_sha(tmp_path) == before          # main untouched, no bogus ref


def test_land_refuses_on_unattested_converged_commit(tmp_path):
    # B5 / REVIEW-B: the converged witness points at a REAL but UN-attested commit.
    # land() must refuse BEFORE any merge AND write NO landed row (so the producer's
    # append-only authorship-attested gate is never poisoned with author=None).
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    # an attested branch tip, BUT the confirmed-work witness sha points elsewhere
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    _git(tmp_path, "checkout", "-q", "peers/run/p1")
    (tmp_path / "u.py").write_text("U")
    _git(tmp_path, "add", "u.py")
    _git(tmp_path, "commit", "-q", "-m", "unattested")     # NO peers-attest note
    unattested = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    assert resolve_author(tmp_path, unattested) is None    # uses the module-level import
    run = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    run.base_sha = _git(tmp_path, "rev-parse", "main").strip()
    run.op_config.landing = "auto-merge"
    led = RunLedger(tmp_path / "p1.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append_attested(tmp_path, tip, event="confirmed-work", subject="F1", status="pass",
        witness={"kind": "git-sha", "uri": unattested, "sha256": unattested},  # -> UNATTESTED
        independence=True, mode_run="p1")
    led.append(event="stop", status="complete", mode_run="p1")
    run._ledger = led
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason in ("unattested-converged", "recheck-failed",
                                                  "no-converged-commit")
    # whichever fail-closed branch fires, NO independence=True/author=None landed row
    landed = [r for r in run.ledger.read() if r.event == "landed"]
    assert not landed                                     # gate NOT poisoned
    assert _target_sha(tmp_path) == before               # main untouched


def test_land_refuses_on_recheck_fail(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    run, tip = _producer_on_branch(tmp_path)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: False, repo=tmp_path)          # fresh recheck FAILS
    assert res.landed is False and res.reason == "recheck-failed"
    assert _target_sha(tmp_path) == before                         # no merge (S3)


def test_land_refuses_on_recheck_exception(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    run, tip = _producer_on_branch(tmp_path)
    def _boom(wt, c): raise RuntimeError("recheck blew up")
    res = land(run, provider=_SameRepoProvider(), target_ref="main", recheck=_boom,
               repo=tmp_path)
    assert res.landed is False and res.reason.startswith("error")   # S5 fail-closed
    assert _target_sha(tmp_path) == before


def test_land_re_detects_self_hosting_on_converged_diff(tmp_path):
    # S4: the converged diff (vs the RUN's recorded base) touches the spine ->
    # re-detected self-hosting at merge time even if the decision said auto-merge.
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "src" / "peers" / "spine").mkdir(parents=True, exist_ok=True)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "src/peers/spine/x.py", "S", peer="claude")
    run = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    run.base_sha = base
    run.op_config.landing = "auto-merge"
    run._ledger = _converged_branch_ledger(tmp_path, tmp_path / "p1.jsonl", "p1", tip)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "self-hosting"     # S4
    assert _target_sha(tmp_path) == before


def test_land_re_detects_self_hosting_against_run_base_not_target(tmp_path):
    # S4 / wrong-base regression: a crafted target_ref whose merge-base with the
    # converged commit sits PAST the spine-touching commit must NOT shrink the
    # detection window. land() diffs the RUN's recorded base..converged, so the
    # spine touch is still seen and the merge is refused, regardless of target_ref.
    _attested_repo(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    (tmp_path / "src" / "peers" / "spine").mkdir(parents=True, exist_ok=True)
    # the spine touch is the FIRST commit on the run branch ...
    _commit_on_branch(tmp_path, "peers/run/p1", "src/peers/spine/x.py", "S", peer="claude")
    # ... then an innocent follow-up that is the converged tip
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "feature.py", "F", peer="claude")
    # craft `main` to sit AT the spine commit so merge-base(main, tip) excludes it
    spine_commit = _git(tmp_path, "rev-parse", "peers/run/p1~1").strip()
    _git(tmp_path, "branch", "main", spine_commit)
    run = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    run.base_sha = base                                  # the REAL fork point (before the spine touch)
    run.op_config.landing = "auto-merge"
    run._ledger = _converged_branch_ledger(tmp_path, tmp_path / "p1.jsonl", "p1", tip)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "self-hosting"     # base..tip still has the spine file


def test_land_refuses_on_undeterminable_base(tmp_path):
    # the run's recorded base is missing/unresolvable -> named fail-closed reason,
    # NOT silently folded into self-hosting (so a future "default base to root" can't
    # turn it into a false negative).
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    run, tip = _producer_on_branch(tmp_path)
    run.base_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"   # not in the repo
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "undeterminable-base"


def test_land_refuses_when_no_converged_commit(tmp_path):
    # an UN-converged ledger (only a dry-round) -> no _converged_commit -> no merge.
    from peers.spine.ledger import RunLedger
    from peers.spine.op_config import OpConfig, load_op_config
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix")
    run = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    run.base_sha = base
    run.op_config.landing = "auto-merge"
    led = RunLedger(tmp_path / "p1.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append(event="dry-round", status="dry", mode_run="p1")
    run._ledger = led
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason in ("no-converged-commit", "not-auto-merge")


def test_land_refuses_on_non_ff_target(tmp_path):
    # the target advanced past base so the CAS ff cannot fast-forward -> merge-conflict,
    # no partial merge (S5).
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    # advance main to a DIVERGENT commit so the converged tip is not a fast-forward
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "diverge.txt").write_text("D")
    _git(tmp_path, "add", "diverge.txt")
    _git(tmp_path, "commit", "-q", "-m", "diverge")
    diverged = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    run, tip = _producer_on_branch(tmp_path)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "merge-conflict"
    assert _target_sha(tmp_path) == diverged                       # target untouched


def test_land_refuses_on_cas_race(tmp_path, monkeypatch):
    # B4 TOCTOU: the target races FORWARD between the ancestry capture and the CAS
    # write. The 4-arg update-ref refuses (rc!=0) and the racer is preserved.
    import peers.spine.auto_merge as am
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    run, tip = _producer_on_branch(tmp_path)
    real_git = am._git
    raced = {"done": False}
    def _racing_git(repo, *args):
        # the moment land() resolves `old` and is about to CAS, advance main to a
        # DIVERGENT commit (simulating a concurrent producer/human push).
        if args[:1] == ("update-ref",) and not raced["done"]:
            raced["done"] = True
            real_git(repo, "checkout", "-q", "main")
            (Path(repo) / "race.txt").write_text("R")
            real_git(repo, "add", "race.txt")
            real_git(repo, "commit", "-q", "-m", "raced")
            real_git(repo, "checkout", "-q", "-")
        return real_git(repo, *args)
    monkeypatch.setattr(am, "_git", _racing_git)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "merge-conflict"   # CAS refused the stale write
    # main is at the RACED commit, NOT clobbered to converged
    assert _target_sha(tmp_path) != tip


def test_land_refuses_on_same_named_tag_at_ancestor(tmp_path):
    # B6 namespace: a TAG `main` at an ancestor co-existing with a divergent branch
    # `main` must not let the ancestry check resolve the tag and clobber the branch.
    # land() resolves refs/heads/main explicitly -> the divergent branch -> non-ff.
    _attested_repo(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "tag", "main", base)                  # tag main at the ANCESTOR
    # make the BRANCH main divergent
    _git(tmp_path, "checkout", "-q", "-b", "main")
    (tmp_path / "d.txt").write_text("D")
    _git(tmp_path, "add", "d.txt")
    _git(tmp_path, "commit", "-q", "-m", "branch-diverge")
    branch_main = _git(tmp_path, "rev-parse", "refs/heads/main").strip()
    _git(tmp_path, "checkout", "-q", "-")
    run, tip = _producer_on_branch(tmp_path)
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "merge-conflict"
    assert _target_sha(tmp_path, "refs/heads/main") == branch_main  # branch NOT clobbered
