"""STEP-7 -- the fleet e2e over REAL git (the §7 + §6.1 verify bar).

Proves the FULL fleet over real ``tmp_path`` git repos + the injected
``FakeSlotRunner`` (no network/LLM/containers): a small DAG
(``find-bugs:reproduce`` -> ``develop`` intra-tool; ``develop`` -> ``develop``
cross-tool over TWO SEPARATE repos via a REAL ``propagate_branch``) validates +
schedules root-first + satisfies the cross-tool dep only after a real propagate;
rejecting the root cascades transitively THROUGH edges the CONDUCTOR recorded
(not hand-fabricated); a same-branch-no-isolation program is rejected; a
self-hosting run routes Tier-2; a ceiling-blocked fleet surfaces Tier-1.
"""
from tests.unit._fleet_helpers import _spec, _program, _fleet_ledger, FakeSlotRunner
from tests.unit._isolation_helpers import (_attested_repo, _commit_on_branch, _run,
                                          _converged_branch_ledger, GitWorktreeProvider)

from peers.fleet.program import validate_program, Program, ModeRunSpec
from peers.fleet.satisfy import dep_satisfied
from peers.fleet.scheduler import startable_runs, Pool, Ceiling
from peers.fleet.invalidate import cascade_invalidate
from peers.fleet.conductor import conduct_tick
from peers.spine.propagate import propagate_branch
from peers.spine.op_config import OpConfig


def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _attested_repo(p)
    return p


def _consumer_tip_on_branch(ws, peer):
    """Commit the consumer's work on ITS leased branch (``ws.branch`` — the worktree
    is already checked out on it) + attest to ``peer``. HONEST-01: the run's
    converged commit must live on its ACTUAL branch so the convergence gate's
    reachability anchor (= consumer_run.branch) holds."""
    from peers import attest
    from tests.unit._isolation_helpers import _git
    wt = ws.worktree_path
    (wt / "use.py").write_text("use")
    _git(wt, "add", "use.py")
    _git(wt, "commit", "-q", "-m", "use")
    tip = _git(wt, "rev-parse", "HEAD").strip()
    attest.attest_commits(wt, peer, ws.base_sha, tip)
    return tip


def test_small_dag_validates_and_schedules_root_first(tmp_path):
    # find-bugs:reproduce on X -> develop on X (intra) ; develop on X -> develop on Y (cross)
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("fbX", tool=x, mode="find-bugs:reproduce"),
        _spec("devX", tool=x, mode="develop", depends_on=["fbX"]),
        ModeRunSpec(tool=y, mode="develop",
                    op_config=OpConfig.from_dict({"mode": "develop"}),
                    run_id="devY", depends_on=["devX"], requires_artifact="git-sha"))
    ok, errors = validate_program(prog)
    assert ok is True and errors == []
    fl = _fleet_ledger(tmp_path)
    # only the root (no deps) is startable first
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(),
                         dep_check=lambda p, c: False)   # deps not yet satisfied
    assert res.startable == ["fbX"]


def test_cross_tool_dep_satisfied_after_real_propagate_two_repos(tmp_path):
    # SEPARATE repos x (producer) and y (consumer) -- distinct ODBs + notes refs.
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    # devX converges on X with a real attested branch tip
    tip = _commit_on_branch(x, "peers/run/devX", "fix.py", "fix", peer="claude")
    devX = _run(x, mode_run="devX", tool=x, branch="peers/run/devX")
    devX._ledger = _converged_branch_ledger(x, x / "devX.jsonl", "devX", tip)
    # devY on Y leases a worktree; a REAL propagate publishes devX's converged tip into it
    with GitWorktreeProvider().lease(y, "devY") as consY:
        res = propagate_branch(devX, consY, repo=x)
        assert res.ok is True
        # devY's OWN converged work, attested to a DISTINCT peer, re-derived from its ledger
        consY_tip = _consumer_tip_on_branch(consY, "codex")
        devY = _run(y, mode_run="devY", tool=consY.worktree_path, branch=consY.branch)
        devY._ledger = _converged_branch_ledger(consY.worktree_path,
                                                consY.worktree_path / "devY.jsonl",
                                                "devY", consY_tip)
        assert dep_satisfied(devX, devY, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=consY) is True


def test_rejecting_root_cascades_through_conductor_recorded_edges(tmp_path):
    # the edges are recorded BY THE CONDUCTOR (not hand-fabricated) -- fbX->devX is
    # INTRA-tool, devX->devY is cross-tool; rejecting fbX revokes BOTH transitively.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    from tests.unit.test_fleet_conductor import _converged_run   # the converged-run helper
    prog = _program(
        _spec("fbX", tool=x, mode="find-bugs:reproduce"),
        _spec("devX", tool=x, mode="develop", depends_on=["fbX"]),
        _spec("devY", tool=x, mode="develop", depends_on=["devX"]))
    fl = _fleet_ledger(x)
    runs = {"fbX": _converged_run(x, "fbX"), "devX": _converged_run(x, "devX")}
    fl.record_status("fbX", "converged")
    fl.record_status("devX", "converged")
    sr = FakeSlotRunner(slots=["s0", "s1", "s2"])
    # devY is scheduled this tick (records devX->devY); devX consumed fbX in a prior
    # tick (pre-seeded converged) -> the conductor records fbX->devX too so the cascade
    # graph is COMPLETE.
    conduct_tick(fl, prog, Pool(slots=["s0", "s1", "s2"]), slot_runner=sr,
                 ceiling=Ceiling(), dep_check=lambda p, c: True,
                 is_self_hosting=lambda r, **kw: (False, ""),
                 runs_by_id=runs, repos_by_id={rid: x for rid in ("fbX", "devX", "devY")})
    edges = {(f, t) for f, t, _a in fl.propagation_edges()}
    assert ("fbX", "devX") in edges and ("devX", "devY") in edges   # conductor-recorded
    assert cascade_invalidate(fl, "fbX") == {"devX", "devY"}        # transitive revoke


def test_rejecting_root_cascades_across_real_propagated_two_repo_dag(tmp_path):
    # Combined STEP-7 bar: the cross-tool edge is satisfied only after a REAL
    # propagate_branch across TWO repos, then the conductor records both the
    # prior intra edge and the starting cross edge so rejecting the root revokes
    # the whole DAG transitively.
    x = _repo(tmp_path, "x")
    y = _repo(tmp_path, "y")
    prog = _program(
        _spec("fbX", tool=x, mode="find-bugs:reproduce"),
        _spec("devX", tool=x, mode="develop", depends_on=["fbX"]),
        ModeRunSpec(tool=y, mode="develop",
                    op_config=OpConfig.from_dict({"mode": "develop"}),
                    run_id="devY", depends_on=["devX"], requires_artifact="git-sha"))
    ok, errors = validate_program(prog)
    assert ok is True and errors == []

    from tests.unit.test_fleet_conductor import _converged_run
    fbX = _converged_run(x, "fbX", peer="claude")
    devX = _converged_run(x, "devX", peer="claude")
    fl = _fleet_ledger(x)
    fl.record_status("fbX", "converged")
    fl.record_status("devX", "converged")

    with GitWorktreeProvider().lease(y, "devY") as consY:
        consY_tip = _consumer_tip_on_branch(consY, "codex")
        devY = _run(y, mode_run="devY", tool=consY.worktree_path, branch=consY.branch)
        devY._ledger = _converged_branch_ledger(consY.worktree_path,
                                                consY.worktree_path / "devY.jsonl",
                                                "devY", consY_tip)
        assert dep_satisfied(devX, devY, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=consY) is False

        propagated = propagate_branch(devX, consY, repo=x)
        assert propagated.ok is True
        assert dep_satisfied(devX, devY, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=consY) is True

        sr = FakeSlotRunner(slots=["s0", "s1", "s2"])
        conduct_tick(
            fl, prog, Pool(slots=["s0", "s1", "s2"]), slot_runner=sr,
            ceiling=Ceiling(),
            dep_check=lambda p, c: dep_satisfied(devX, devY, producer_repo=x,
                                                 consumer_repo=y, kind="cross",
                                                 consumer_ws=consY)
            if (p, c) == ("devX", "devY") else True,
            is_self_hosting=lambda r, **kw: (False, ""),
            runs_by_id={"fbX": fbX, "devX": devX},
            repos_by_id={"fbX": x, "devX": x, "devY": y},
        )

    edges = {(f, t) for f, t, _a in fl.propagation_edges()}
    assert ("fbX", "devX") in edges and ("devX", "devY") in edges
    assert cascade_invalidate(fl, "fbX") == {"devX", "devY"}


def test_same_branch_no_isolation_program_is_rejected(tmp_path):
    x = _repo(tmp_path, "x")
    prog = Program(runs=[
        _spec("dup", tool=x, writable=True),
        _spec("dup", tool=x, mode="research", writable=True)])  # same branch, no isolation
    ok, errors = validate_program(prog)
    assert ok is False
    assert any("same branch" in e or "duplicate" in e for e in errors)


def test_self_hosting_run_routes_tier2(tmp_path):
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    tip = _commit_on_branch(x, "peers/run/a", "fix.py", "fix", peer="claude")
    a = _run(x, mode_run="a", tool=x, branch="peers/run/a")
    a._ledger = _converged_branch_ledger(x, x / "a.jsonl", "a", tip)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "a"}, liveness={"a": "done"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr, ceiling=Ceiling(),
                       dep_check=lambda p, c: True, runs_by_id={"a": a}, repos_by_id={"a": x},
                       is_self_hosting=lambda r, **kw: (True, "target-is-peers"),
                       changed_paths_of=lambda rid: ["src/peers/spine/gates.py"])
    assert "a" in rep.tier2 and "a" not in rep.tier1


def test_ceiling_blocked_fleet_surfaces_tier1(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, max_tokens=80), _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0", "s1"], world={"s0": "a"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(max_tokens=150), dep_check=lambda p, c: True,
                       is_self_hosting=lambda r, **kw: (False, ""),
                       projected={"a": 80, "b": 100})
    assert any("ceiling" in str(t) for t in rep.tier1)
    assert "b" not in rep.started                       # never overspent
