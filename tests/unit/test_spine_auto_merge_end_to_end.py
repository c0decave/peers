"""STEP-6 — end-to-end: the full decision -> execution chain over real git.

Proves the §6.1 Stage 6 verify bar across Tasks 1-5 (no production change here):
(1) a trusted converged run with a passing recheck auto-merges its CONVERGED
commit into the target ref; (2) a spine-touching run is self-hosting -> branch-pr,
no merge; (3) a recheck-fail -> no merge; (4) detection fail-safe (uncertain ->
self-hosting); (5) a branch advanced past convergence to an un-attested commit ->
the ATTESTED CONVERGED commit is merged, never the advanced tip, and the recheck
ran against the converged TREE (the S3 TOCTOU regression).
"""
import subprocess
from tests.unit._isolation_helpers import (_git, _attested_repo, _commit_on_branch,
                                            _run, _converged_branch_ledger)
from tests.unit.test_spine_auto_merge import _SameRepoProvider   # reuse the recheck provider

from peers.spine.auto_merge import land
from peers.spine.self_hosting import is_self_hosting
from peers.spine.authorship import resolve_author


def _target_sha(repo, ref="refs/heads/main"):
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _converged_run(tmp_path, mode_run, file, content, landing="auto-merge", peer="claude"):
    base = _git(tmp_path, "rev-parse", "HEAD").strip()              # the run's fork point
    tip = _commit_on_branch(tmp_path, f"peers/run/{mode_run}", file, content, peer=peer)
    run = _run(tmp_path, mode_run=mode_run, branch=f"peers/run/{mode_run}")
    run.base_sha = base                                            # the recorded base (S4)
    run.op_config.landing = landing
    run._ledger = _converged_branch_ledger(tmp_path, tmp_path / f"{mode_run}.jsonl",
                                           mode_run, tip)
    return run, tip


def test_trusted_run_allowed_to_auto_merge_after_recheck(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    run, tip = _converged_run(tmp_path, "r1", "feature.py", "F")     # non-governance file
    seen = {}
    def _recheck(worktree, commit):
        seen["args"] = (commit,)                                    # the recheck saw the converged commit
        # the recheck worktree is a fresh detached checkout AT the converged commit
        seen["wt_head"] = _git(worktree, "rev-parse", "HEAD").strip()
        return True
    res = land(run, provider=_SameRepoProvider(), target_ref="main", recheck=_recheck,
               repo=tmp_path)
    assert res.landed is True and res.merged_sha == tip
    assert res.target_ref == "refs/heads/main"                      # canonical ref
    assert seen["args"] == (tip,) and seen["wt_head"] == tip        # rechecked the converged TREE
    assert _target_sha(tmp_path) == tip                             # landed only after recheck
    assert resolve_author(tmp_path, tip) == "claude"               # attestation preserved (no re-attest)


def test_spine_touching_run_is_branch_pr_no_merge(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    (tmp_path / "src" / "peers" / "spine").mkdir(parents=True, exist_ok=True)
    before = _target_sha(tmp_path)
    run, tip = _converged_run(tmp_path, "r1", "src/peers/spine/x.py", "S")  # spine touch
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: True, repo=tmp_path)
    assert res.landed is False and res.reason == "self-hosting"
    assert _target_sha(tmp_path) == before                          # forced branch-pr -> no merge


def test_recheck_fail_blocks_merge(tmp_path):
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    before = _target_sha(tmp_path)
    run, tip = _converged_run(tmp_path, "r1", "feature.py", "F")
    res = land(run, provider=_SameRepoProvider(), target_ref="main",
               recheck=lambda wt, c: False, repo=tmp_path)
    assert res.landed is False and res.reason == "recheck-failed"
    assert _target_sha(tmp_path) == before


def test_detection_fail_safe_uncertain_is_self_hosting(tmp_path):
    # an undeterminable diff (None changed_paths) -> self-hosting -> no auto-merge.
    assert is_self_hosting(tmp_path, changed_paths=None)[0] is True


def test_advanced_branch_toctou_merges_attested_converged_not_tip(tmp_path):
    # S3 TOCTOU regression: after convergence the producer ADVANCES its branch to an
    # un-attested commit. land() must merge the ATTESTED converged commit recorded in
    # the ledger, never the live (un-attested) branch tip -- and the recheck runs
    # against the CONVERGED tree, not the advanced tip.
    _attested_repo(tmp_path)
    _git(tmp_path, "branch", "main")
    run, converged_tip = _converged_run(tmp_path, "r1", "feature.py", "F")
    # advance peers/run/r1 PAST convergence to an UN-attested commit
    _git(tmp_path, "checkout", "-q", "peers/run/r1")
    (tmp_path / "extra.py").write_text("E")
    _git(tmp_path, "add", "extra.py")
    _git(tmp_path, "commit", "-q", "-m", "post-convergence (unattested)")
    advanced_tip = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    assert advanced_tip != converged_tip
    assert resolve_author(tmp_path, advanced_tip) is None           # the advanced tip is UN-attested
    seen = {}
    def _recheck(worktree, commit):
        seen["wt_head"] = _git(worktree, "rev-parse", "HEAD").strip()
        return True
    res = land(run, provider=_SameRepoProvider(), target_ref="main", recheck=_recheck,
               repo=tmp_path)
    assert res.landed is True and res.merged_sha == converged_tip   # the LEDGER-bound sha, not the tip
    assert seen["wt_head"] == converged_tip                         # rechecked the converged tree, NOT advanced
    assert _target_sha(tmp_path) == converged_tip                  # merged the converged commit
    assert _target_sha(tmp_path) != advanced_tip                  # NEVER the advanced tip
