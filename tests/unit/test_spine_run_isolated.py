from tests.unit._isolation_helpers import FakeWorktreeProvider, _attested_repo

from peers.spine.mode_run import run_isolated
from peers.spine.op_config import OpConfig


class _NoopFE:
    def prepare(self, run): pass
    def run(self, run):
        run.ledger.append(event="dry-round", status="dry", mode_run=run.mode_run)
    def interpret(self, run):
        # `base_sha` is read via getattr so this asserts the FIELD exists AND is
        # threaded -- before STEP-3 the attribute is absent (sentinel "MISSING").
        return {"tool": str(run.tool), "branch": run.branch,
                "base_sha": getattr(run, "base_sha", "MISSING")}


class _BoomFE:
    def prepare(self, run): pass
    def run(self, run): raise RuntimeError("boom")     # NOT a ledger error -> escapes drive()
    def interpret(self, run): return {}


def test_run_isolated_drives_inside_the_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    out = run_isolated(repo, OpConfig.from_dict({"mode": "develop"}), "r1",
                       _NoopFE(), prov)
    assert out["branch"] == "peers/run/r1"
    assert out["tool"] != str(repo)                     # ran in the worktree, not the shared repo
    assert prov.leased == ["r1"] and prov.released == ["r1"]   # leased AND torn down


def test_two_runs_no_clobber_and_both_released(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    run_isolated(repo, OpConfig.from_dict({"mode": "develop"}), "r1", _NoopFE(), prov)
    run_isolated(repo, OpConfig.from_dict({"mode": "research"}), "r2", _NoopFE(), prov)
    assert prov.leased == ["r1", "r2"]
    assert prov.released == ["r1", "r2"]                # both fault domains torn down


def test_run_isolated_threads_the_lease_base_sha_onto_the_run(tmp_path):
    # STEP-3 (S4 / wrong-base major): the run's recorded fork point
    # (RunWorkspace.base_sha, captured at lease time) MUST be threaded onto
    # ModeRun.base_sha so land()/develop diff base..converged against the REAL
    # fork commit -- never merge-base(HEAD, ...) (EMPTY in run_isolated).
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    out = run_isolated(repo, OpConfig.from_dict({"mode": "develop"}), "r1",
                       _NoopFE(), prov, base=head)
    assert out["base_sha"] == head           # threaded from the lease, not absent/None


def test_run_isolated_base_sha_is_the_lease_default_when_no_base(tmp_path):
    # edge: no explicit `base` -> the (fake) lease records "0"*40 as its fork point;
    # run_isolated threads whatever the lease captured, faithfully (no silent None).
    repo = tmp_path / "repo"
    repo.mkdir()
    _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    out = run_isolated(repo, OpConfig.from_dict({"mode": "develop"}), "r1",
                       _NoopFE(), prov)
    assert out["base_sha"] == "0" * 40


def test_modeRun_base_sha_defaults_to_none(tmp_path):
    # legacy single-HEAD path: a ModeRun constructed without a lease has no recorded
    # fork point -> base_sha None (land() then refuses auto-merge: undeterminable-base).
    from peers.spine.mode_run import ModeRun
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    assert run.base_sha is None
    assert run.branch is None                # the existing legacy default, unchanged


def test_lease_released_even_when_frontend_raises(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    try:
        run_isolated(repo, OpConfig.from_dict({"mode": "develop"}), "r1", _BoomFE(), prov)
    except RuntimeError:
        pass
    assert prov.released == ["r1"]                       # finally-released despite the raise
