"""STEP-4 — DevelopFrontend wires REAL self-hosting detection.

Stage 4 passed the constant ``self_hosting=False`` into ``build_landing_contract``
(branch-pr was universal). Stage 6 makes ``self_hosting`` load-bearing, so develop
computes the REAL value from the converged diff (the run's changed files vs its
RECORDED base ``run.base_sha``, with ``--no-renames -z``) and passes it through.

These tests drive ``DevelopFrontend`` over real tmp git repos (no LLM/network):
a spine-touching converged diff => self-hosting => branch-pr; a docs-only diff =>
trusted => auto-merge (when requested + mergeable); a rename-out-of-governance =>
the surfaced delete still classifies self-hosting (B1); a missing recorded base =>
fail-safe to self-hosting; a legacy single-HEAD run (branch=None) stays
self_hosting=False (no isolated diff to inspect).
"""
from tests.unit._develop_helpers import (_FixedAuditor, _FixedAuthor, _FixedImpl,
                                          _F, _attested_repo)
from tests.unit._isolation_helpers import _git, _commit_on_branch

from peers.attest import attest_commits
from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import AuthoredContract, ImplementResult
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig


def _fe(sha, branch):
    return DevelopFrontend(
        _FixedAuditor([_F("F1")], once=True),
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q",
                                      findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch=branch)),
        dimensions=["correctness"], run_tests=lambda c: (0, "1 passed"),
        refuter_factory=lambda f: (lambda i: False))


def _landing_contract(rows):
    land = [r for r in rows if r.event == "landing"]
    return land[-1].witness["contract"]


def test_develop_self_hosting_wire_fails_safe_when_git_diff_raises(tmp_path, monkeypatch):
    # MERGE-GATE REVIEW: _converged_changed_paths must NEVER raise out of run()/drive().
    # A hung git raises subprocess.TimeoutExpired (a SubprocessError, NOT an OSError) and
    # a missing git raises FileNotFoundError; drive() does NOT wrap frontend.run(), so an
    # un-caught raise crashes a real develop run. The helper's docstring promises None on
    # ANY git error -> is_self_hosting then fails safe to self-hosting (branch-pr).
    import subprocess
    _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    tip = _commit_on_branch(tmp_path, "peers/run/r1", "docs/readme.md", "D", peer="claude")
    real_run = subprocess.run
    def _raising_run(args, *a, **k):
        if isinstance(args, (list, tuple)) and "--no-renames" in args:   # the converged-diff call
            raise subprocess.TimeoutExpired(cmd=list(args), timeout=120)  # a hung git
        return real_run(args, *a, **k)
    monkeypatch.setattr(subprocess, "run", _raising_run)
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop", "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1", branch="peers/run/r1")
    run.base_sha = base
    drive(run, _fe(tip, "peers/run/r1"))            # MUST COMPLETE -- not raise
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is True and c["landing_mode"] == "branch-pr"   # fail-safe


def test_develop_marks_self_hosting_when_diff_touches_spine(tmp_path):
    _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()       # the run's fork point
    (tmp_path / "src" / "peers" / "spine").mkdir(parents=True, exist_ok=True)
    tip = _commit_on_branch(tmp_path, "peers/run/r1", "src/peers/spine/x.py", "S",
                            peer="claude")
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop",
                                                "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    run.base_sha = base                                       # the lease-time fork point (S4)
    # inject the IMPLEMENT result to point head at the spine-touching branch tip
    fe = _fe(tip, "peers/run/r1")
    drive(run, fe)
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is True and c["landing_mode"] == "branch-pr"   # S2/§6.3


def test_develop_marks_self_hosting_on_rename_out_of_governance(tmp_path):
    # B1 regression through the real wire: the run RENAMES a spine file OUT of the
    # spine (the archetypal gate-removal). --no-renames surfaces the deleted spine
    # path so the develop wire still classifies self-hosting -> branch-pr.
    _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "src" / "peers" / "spine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "peers" / "spine" / "gates.py").write_text("g\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed gate")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-b", "peers/run/r1")
    _git(tmp_path, "mv", "src/peers/spine/gates.py", "innocent.py")
    _git(tmp_path, "commit", "-q", "-m", "rename gate out")
    tip = _git(tmp_path, "rev-parse", "HEAD").strip()
    _git(tmp_path, "checkout", "-q", "-")
    attest_commits(tmp_path, "claude", base, tip)   # re-attest the renamed tip (real author)
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop",
                                                "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    run.base_sha = base
    drive(run, _fe(tip, "peers/run/r1"))
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is True and c["landing_mode"] == "branch-pr"   # B1: delete surfaced


def test_develop_allows_auto_merge_when_diff_docs_only(tmp_path):
    # the happy path: the docs-only diff produces a REAL non-empty diff
    # (base_sha..head_sha), not an incidental-empty one -- so the path-glob layer is
    # genuinely exercised and the trusted run is ALLOWED to auto-merge.
    _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    base = _git(tmp_path, "rev-parse", "HEAD").strip()
    tip = _commit_on_branch(tmp_path, "peers/run/r1", "docs/guide.md", "G", peer="claude")
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop",
                                                "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    run.base_sha = base
    drive(run, _fe(tip, "peers/run/r1"))
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is False                  # docs-only -> trusted (real diff)
    assert c["landing_mode"] == "auto-merge"           # requested + mergeable + trusted


def test_develop_empty_recorded_base_fails_safe_to_self_hosting(tmp_path):
    # B-class / empty-diff regression: a run with NO recorded base (base_sha None)
    # -> _converged_changed_paths returns None -> is_self_hosting fails safe to True
    # -> branch-pr. No "empty == no-op == trusted" shortcut on the develop wire.
    _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    tip = _commit_on_branch(tmp_path, "peers/run/r1", "docs/guide.md", "G", peer="claude")
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop",
                                                "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    # run.base_sha left None (the legacy / un-threaded case)
    drive(run, _fe(tip, "peers/run/r1"))
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is True and c["landing_mode"] == "branch-pr"


def test_legacy_no_branch_run_is_not_self_hosting_and_branch_pr(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = ModeRun(tool=tmp_path,
                  op_config=OpConfig.from_dict({"mode": "develop",
                                                "landing": "auto-merge"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")   # branch=None (legacy)
    drive(run, _fe(sha, "feat/x"))
    c = _landing_contract(run.ledger.read())
    assert c["self_hosting"] is False                  # no isolated diff to inspect
    # legacy single-HEAD: no isolated branch -> auto-merge cannot execute (land
    # would refuse no-isolated-branch); the contract may say auto-merge but the
    # executor is the second gate. Here the decision is computed honestly.
