from tests.unit._isolation_helpers import FakeWorktreeProvider, _attested_repo

from peers.develop.adapters import ContractImplementer, worktree_convergence
from peers.develop.ports import AuthoredContract

_PLAN = ("# Fix\n\n## Meta\nsurfaces: [lib]\nacceptance: pytest -q\n\n"
         "## Steps\n- [ ] [STEP-1] do it\n  - touches: src/x.py\n")


def test_worktree_convergence_runs_inner_in_a_lease(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha = _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    seen = {}

    def _inner(worktree_path):
        seen["wt"] = worktree_path                       # ran INSIDE the leased worktree
        return (True, sha, "peers/run/conv")

    rc = worktree_convergence(prov, _inner)
    impl = ContractImplementer(run_convergence=rc)
    res = impl.implement(AuthoredContract(plan_md=_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), repo)
    assert res.ok and res.head_sha == sha
    assert "wt-" in str(seen["wt"])                      # the inner ran in the lease, not the repo
    assert prov.released                                 # lease torn down


def test_worktree_convergence_releases_on_non_convergence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _attested_repo(repo)
    prov = FakeWorktreeProvider(tmp_path / "leases")
    rc = worktree_convergence(prov, lambda wt: (False, None, None))
    impl = ContractImplementer(run_convergence=rc)
    res = impl.implement(AuthoredContract(plan_md=_PLAN, acceptance="pytest -q",
                                          findings=["F1"]), repo)
    assert res.ok is False
    assert prov.released                                 # released despite non-convergence
