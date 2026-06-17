"""ACTIVE tests for the ``peers fleet`` operator command + the fleet honesty seam.

Plan: ``docs/plans/2026-06-15-new-feature-active-test-plans.md`` lines 347-429
(cases FB-H1/H2/S1/S2/S3/CB4/E1/E2, re-prefixed FLEET-* here to avoid id collision
with the find-bugs area's FB-* ids).

Every test drives the REAL deterministic seam — no live LLM, no container:

  * the operator CLI ``peers_ctl.cli.cmd_fleet`` (manifest intake + dry-run + the
    fail-closed exit-code contract);
  * the full spawn -> lease -> drive -> converge -> persist path via the REAL
    ``ProcessSlotRunner`` + a no-LLM frontend builder injected through the
    documented ``PEERS_FLEET_BUILDERS`` env hook + ``conduct_fleet``;
  * the cross-repo independence seam ``satisfy.dep_satisfied`` (re-derives
    producer!=consumer from each repo's ``refs/notes/peers-attest``, never a
    propagation row field);
  * CB-4 (``DevelopFrontend.prepare`` binds the bar runner to ``run.tool`` — the
    leased worktree — not the construction repo).

The HONESTY CHECKS are the load-bearing assertions: trust is RE-DERIVED from the
substrate (real git diff + an attested commit reachable from the chosen head +
author re-resolved from ``refs/notes/peers-attest``), never a self-reported row.
"""
from __future__ import annotations

import json
import os
import time

import yaml

from peers.fleet.daemon import conduct_fleet
from peers.fleet.satisfy import dep_satisfied
from peers.fleet.scheduler import Ceiling
from peers.fleet.slot_runner import ProcessSlotRunner
from peers.spine.auto_merge import LandingResult
from peers.spine.authorship import resolve_author
from peers.spine.propagate import propagate_branch
from peers.spine.worktree import GitWorktreeProvider
from peers_ctl.cli import cmd_fleet
from tests.unit._isolation_helpers import (
    _attested_repo, _commit_on_branch, _converged_branch_ledger, _git, _run)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _attested_repo(p)
    return p


def _two_repo_manifest(tmp_path, repoX, repoY):
    """A 2-run manifest mirroring the plan's FB-H1 smallest target."""
    raw = {
        "pool": {"slots": ["s0", "s1"]},
        "ceiling": {},
        "daemon": {"target_ref": "main", "max_ticks": 5, "tick_sleep_s": 1,
                   "idle_timeout_s": 120},
        "runs": [
            {"run_id": "devX", "tool": str(repoX), "mode": "develop",
             "landing": "branch-pr", "budget": {"max_rounds": 1}},
            {"run_id": "devY", "tool": str(repoY), "mode": "develop",
             "depends_on": ["devX"], "requires_artifact": "git-sha",
             "budget": {"max_rounds": 1}},
        ],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _single_develop_manifest(tmp_path, repoX, *, landing="branch-pr", max_ticks=5):
    raw = {
        "pool": {"slots": ["s0"]},
        "ceiling": {},
        "daemon": {"target_ref": "main", "max_ticks": max_ticks, "tick_sleep_s": 1,
                   "idle_timeout_s": 120},
        "runs": [
            {"run_id": "devX", "tool": str(repoX), "mode": "develop",
             "landing": landing, "budget": {"max_rounds": 1}},
        ],
    }
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _spawn_fleet(tmp_path, manifest_path, builders_module, *, monkeypatch,
                 is_self_hosting=None, lander=None, max_ticks=6):
    """Drive the REAL ProcessSlotRunner + conduct_fleet over a manifest, with a
    no-LLM frontend builder wired through PEERS_FLEET_BUILDERS. Returns
    ``(res, fl, ProcessSlotRunner)`` — the live path the operator command runs."""
    from peers.fleet.fleet_ledger import FleetLedger
    from peers.fleet.manifest import load_fleet_manifest

    # the spawned run_one child must import the builder module -> repo root on PYTHONPATH.
    monkeypatch.setenv("PYTHONPATH", _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("PEERS_FLEET_BUILDERS", builders_module)

    raw = yaml.safe_load(manifest_path.read_text())
    m = load_fleet_manifest(raw)
    fl = FleetLedger(tmp_path / "fleet.jsonl")
    psr = ProcessSlotRunner(m.pool, m.repos_by_id, idle_timeout_s=120)
    kw = {}
    if is_self_hosting is not None:
        kw["is_self_hosting"] = is_self_hosting
    if lander is not None:
        kw["lander"] = lander
    try:
        res = conduct_fleet(
            fl, m.program, m.pool, Ceiling(), slot_runner=psr,
            repos_by_id=m.repos_by_id, dep_check=lambda p, c: True,
            target_ref="main", max_ticks=max_ticks, tick_sleep_s=0.2,
            sleep=time.sleep, **kw)
    finally:
        psr.shutdown()
    return res, fl, psr


# ==========================================================================
# FLEET-H1 (happy) — dry-run validates + prints the plan, spawns nothing.
# ==========================================================================
def test_fleet_h1_dry_run_validates_and_prints_plan(tmp_path, capsys):
    repoX = _repo(tmp_path, "repoX")
    repoY = _repo(tmp_path, "repoY")
    mpath = _two_repo_manifest(tmp_path, repoX, repoY)

    spawned = {"n": 0}

    def fake_conduct(*a, **k):           # must NEVER be called on a dry-run
        spawned["n"] += 1
        raise AssertionError("dry-run must not enter the conductor loop")

    class _SR:
        def shutdown(self):
            pass

    rc = cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", dry_run=True,
                   _conduct=fake_conduct, _make_slot_runner=lambda m: _SR())
    out = capsys.readouterr().out
    assert rc == 0
    assert "manifest OK — 2 run(s), 2 slot(s)" in out
    assert f"- devX: develop on {repoX} (deps: -, landing: branch-pr)" in out
    assert "- devY: develop on" in out and "deps: ['devX']" in out
    # HONESTY: a dry-run does no work + lands nothing -> no fleet ledger persisted
    # and the conductor was never entered (it cannot fabricate convergence).
    assert spawned["n"] == 0
    assert not (tmp_path / "fleet.jsonl").exists()


# ==========================================================================
# FLEET-H2 (happy) — a full deterministic fleet run spawns the real run_one
# child, drives a no-LLM frontend to a REAL attested commit, persists the
# converged tip under refs/peers/fleet/<id>, conductor RE-VERIFIES from substrate.
# ==========================================================================
def test_fleet_h2_real_run_persists_attested_converged_tip(tmp_path, monkeypatch):
    repoX = _repo(tmp_path, "repoX")
    mpath = _single_develop_manifest(tmp_path, repoX, landing="branch-pr")

    res, fl, _psr = _spawn_fleet(
        tmp_path, mpath, "tests.unit._fleet_builder_dev",
        monkeypatch=monkeypatch, max_ticks=8)

    assert res.cause == "complete", f"got {res.cause}: {res.statuses} err={res.error}"
    assert fl.latest_status("devX") == "converged"

    # HONESTY (substrate, not the ledger row): the run's REAL tip is pinned under
    # refs/peers/fleet/devX and resolves to a 40-hex commit.
    ref = _git(repoX, "rev-parse", "refs/peers/fleet/devX").strip()
    assert len(ref) == 40

    # the persisted record binds converged_commit == real tip and records the ref.
    rec = json.loads((repoX / ".peers" / "fleet-runs" / "devX" / "record.json").read_text())
    assert rec["converged_commit"] == ref
    assert rec["ref"] == "refs/peers/fleet/devX"

    # that commit carries a peers-attest note re-resolving to the producing peer.
    note = _git(repoX, "notes", "--ref=peers-attest", "show", ref)
    assert "claude" in note
    assert resolve_author(repoX, ref) == "claude"

    # HONESTY (negative control): the converged verdict is re-derived from the
    # substrate, so deleting the attest note makes the run NO LONGER converged.
    from peers.spine.propagate import is_converged
    from peers.spine.ledger import RunLedger
    rows = RunLedger(repoX / ".peers" / "fleet-runs" / "devX" / "run.jsonl").read()
    assert is_converged(rows, mode_run="devX", repo=repoX, head="refs/peers/fleet/devX")
    _git(repoX, "notes", "--ref=peers-attest", "remove", ref)
    assert not is_converged(rows, mode_run="devX", repo=repoX, head="refs/peers/fleet/devX")


# ==========================================================================
# FLEET-S1 (sad) — a develop fleet spec missing its 'develop' config block
# FAILS CLOSED (UnsupportedFleetMode), never a silent degraded run.
# ==========================================================================
def test_fleet_s1_missing_develop_block_fails_closed(tmp_path, monkeypatch):
    repoX = _repo(tmp_path, "repoX")

    # (a) the run_one child itself: with the DEFAULT in-tree builders, a develop
    #     spec WITHOUT a 'develop' block fails closed at exit 2 (UnsupportedFleetMode).
    from peers.fleet import run_one
    monkeypatch.delenv("PEERS_FLEET_BUILDERS", raising=False)
    run_one._FRONTEND_BUILDERS.clear()
    run_one._load_env_builders("peers.fleet.builders")     # the real in-tree builders
    head = _git(repoX, "rev-parse", "HEAD").strip()
    spec_json = json.dumps({
        "run_id": "devX", "tool": str(repoX), "mode": "develop",
        "op_config": {"mode": "develop"}, "base_sha": head,
        "branch": "peers/run/devX"})         # NO 'develop' config block
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = run_one.main(["--spec", spec_json])
    run_one._FRONTEND_BUILDERS.clear()
    assert rc == 2                                          # caller error, fail-closed
    assert "develop fleet spec needs a 'develop' config block" in err.getvalue()

    # (b) the whole fleet via the REAL operator CLI (default PEERS_FLEET_BUILDERS =
    #     peers.fleet.builders): the spawned child fails closed -> the run never
    #     converges -> the CLI returns 1 (work remains). Real conduct + real
    #     ProcessSlotRunner, no injection seam.
    monkeypatch.setenv("PYTHONPATH", _REPO_ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("PEERS_FLEET_BUILDERS", raising=False)   # let cmd_fleet default it
    mpath = _single_develop_manifest(tmp_path, repoX, landing="branch-pr", max_ticks=3)
    rc = cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", max_ticks=3)
    # The child fails closed (exit 2 -> conductor records 'failed', a TERMINAL
    # status), so res.ok is False -> the CLI returns 1. (The loop's cause is
    # 'complete' only in the all-terminal sense; res.ok / the exit code is the
    # contract, NOT the cause string.)
    assert rc == 1
    from peers.fleet.fleet_ledger import FleetLedger
    fl = FleetLedger(tmp_path / "fleet.jsonl")
    assert fl.latest_status("devX") == "failed"
    assert fl.latest_status("devX") != "converged"
    # HONESTY: no converged tip was ever pinned (a missing config degrades to an
    # honest non-converged terminal, never a fabricated pass).
    pin = _git_quiet(repoX, "rev-parse", "--verify", "--quiet", "refs/peers/fleet/devX")
    assert pin is None


def _git_quiet(repo, *args):
    r = __import__("subprocess").run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True)
    out = r.stdout.strip()
    return out if (r.returncode == 0 and out) else None


# ==========================================================================
# FLEET-S2 (sad) — a LYING / NO-OP agent (claims success, makes NO real diff)
# CANNOT forge a converged result; the run degrades to honest non-convergence.
# ==========================================================================
def test_fleet_s2_noop_agent_cannot_forge_convergence(tmp_path, monkeypatch):
    repoX = _repo(tmp_path, "repoX")
    head_before = _git(repoX, "rev-parse", "HEAD").strip()
    mpath = _single_develop_manifest(tmp_path, repoX, landing="branch-pr", max_ticks=5)

    res, fl, _psr = _spawn_fleet(
        tmp_path, mpath, "tests.unit._active_fleet_builder_noop",
        monkeypatch=monkeypatch, max_ticks=5)

    # the no-op agent never converges: the conductor re-derives from the (empty)
    # substrate, so the run is NOT converged and the fleet's honest terminal is
    # res.ok is False (a forged 'converged' here would be the failure). The run
    # reaches a terminal 'failed' status (the conductor's re-verify rejected it),
    # so res.cause is 'complete' only in the all-terminal sense — res.ok is the
    # contract.
    assert res.ok is False
    assert fl.latest_status("devX") != "converged"
    assert fl.latest_status("devX") in ("failed", "rejected")
    assert "devX" not in res.landed

    # HONESTY (substrate): no NEW attested commit exists; if a fleet ref was pinned
    # at all it can only resolve to the unchanged base (no confirmed-work git-sha
    # row -> AgentConvergenceRunner / the gate produced nothing landable).
    pin = _git_quiet(repoX, "rev-parse", "--verify", "--quiet", "refs/peers/fleet/devX")
    assert pin in (None, head_before)
    # and no record claims a converged_commit.
    rec_path = repoX / ".peers" / "fleet-runs" / "devX" / "record.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text())
        assert rec.get("converged_commit") is None


# ==========================================================================
# FLEET-S3 (sad) — cross-repo independence: producer & consumer authored by the
# SAME peer must NOT satisfy the cross-tool dep (re-derived from the substrate
# notes ref, never the propagation.independence field).
# ==========================================================================
def test_fleet_s3_same_peer_cross_dep_not_satisfied(tmp_path):
    x = _repo(tmp_path, "x")          # producer repo
    y = _repo(tmp_path, "y")          # consumer repo

    # devX converges on X with a real attested branch tip authored by 'claude'.
    tipX = _commit_on_branch(x, "peers/run/devX", "fix.py", "fix", peer="claude")
    devX = _run(x, mode_run="devX", tool=x, branch="peers/run/devX")
    devX._ledger = _converged_branch_ledger(x, x / "devX.jsonl", "devX", tipX)

    from peers import attest

    def _consumer_devY(consY, peer):
        """The consumer's OWN converged work on its leased branch, attested to
        ``peer`` BEFORE the converged ledger is built (so the row's baked author
        matches the substrate note — the authorship-attested gate compares them).
        Returns ``(devY, tip)``."""
        wt = consY.worktree_path
        (wt / "use.py").write_text("use")
        _git(wt, "add", "use.py")
        _git(wt, "commit", "-q", "-m", "use")
        tip = _git(wt, "rev-parse", "HEAD").strip()
        attest.attest_commits(wt, peer, consY.base_sha, tip)
        devY = _run(y, mode_run="devY", tool=wt, branch=consY.branch)
        devY._ledger = _converged_branch_ledger(wt, wt / "devY.jsonl", "devY", tip)
        return devY, tip

    # ---- SAME-PEER end: devY also authored by 'claude' -> NOT satisfied ----
    with GitWorktreeProvider().lease(y, "devY") as consY:
        # a REAL propagate publishes devX's converged tip into the consumer worktree
        # (pins refs/propagated/devX) — the discriminating PROPAGATED proof.
        assert propagate_branch(devX, consY, repo=x).ok is True
        devY, consY_tip = _consumer_devY(consY, "claude")

        # HONESTY: dep_satisfied re-resolves producer_peer in x AND consumer_peer in
        # y over a tip RE-DERIVED from the consumer's OWN ledger, and REJECTS the
        # same-peer cross-run handoff (satisfy.py:117-118). Both ends -> 'claude'.
        assert resolve_author(x, tipX) == "claude"
        assert resolve_author(y, consY_tip) == "claude"
        assert dep_satisfied(devX, devY, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=consY) is False

    # ---- CONTRAST (the gate is real, not always-False): a DISTINCT consumer peer
    #      ('codex') over the SAME producer SATISFIES. Fresh lease so the consumer
    #      tip is attested to codex from the start (matching its baked ledger row). ----
    with GitWorktreeProvider().lease(y, "devY") as consY2:
        assert propagate_branch(devX, consY2, repo=x).ok is True
        devY2, consY2_tip = _consumer_devY(consY2, "codex")
        assert resolve_author(x, tipX) == "claude"
        assert resolve_author(y, consY2_tip) == "codex"
        assert dep_satisfied(devX, devY2, producer_repo=x, consumer_repo=y,
                             kind="cross", consumer_ws=consY2) is True


# ==========================================================================
# FLEET-CB4 (edge) — develop's quality bar is bound to run.tool (the leased
# worktree), NOT the construction repo.
# ==========================================================================
def test_fleet_cb4_bar_bound_to_leased_worktree_not_construction_repo(tmp_path):
    from peers.develop.assembly import make_develop_frontend
    from peers.spine.ledger import RunLedger
    from peers.spine.mode_run import ModeRun
    from peers.spine.op_config import OpConfig

    # construction repo ROOT: NO detectable test runner -> _detect_runner -> None.
    construction = tmp_path / "construction"
    construction.mkdir()
    # leased WORKTREE (run.tool): has a detectable runner (go.mod -> "go test ./...").
    worktree = tmp_path / "leased_wt"
    worktree.mkdir()
    (worktree / ".peers").mkdir()                  # the run ledger lives under here
    (worktree / "go.mod").write_text("module x\n")

    # build the REAL develop frontend with NO explicit run_tests -> assembly threads
    # the per-run factory (builders.py:61-63 path); the construction repo is passed.
    def _agent(prompt):
        return "{}"

    fe = make_develop_frontend(
        construction, run_agent=_agent, impl_run_agent=lambda p, w: "{}",
        dimensions=["correctness"], convergence_budget=2, attest_peer="claude")

    # HONESTY: the factory is wired (no explicit run_tests) and prepare binds the bar
    # runner to run.tool at CALL time (frontend.py:132-144).
    assert fe.run_tests_factory is not None

    ledger = RunLedger(worktree / ".peers" / "run.jsonl")
    run = ModeRun(tool=worktree, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=worktree / ".peers" / "run.jsonl",
                  mode_run="devX", branch="peers/run/devX", base_sha="0" * 40)
    fe.prepare(run)

    # HONESTY (the load-bearing assertion): the bar runner detected the WORKTREE's
    # runner (go.mod -> "go test ./..."), proving prepare bound the bar to run.tool.
    # The construction root has NO runner, so a construction-bound bar would have
    # recorded command=None / bar:absent. The detected COMMAND is the deterministic
    # substrate proof (independent of whether `go` actually runs here).
    rows = ledger.read()
    inferred = [r for r in rows if r.event == "bar-inferred"]
    assert inferred, "prepare must record a bar-inferred row"
    witness = inferred[-1].witness
    assert isinstance(witness, dict)
    assert witness.get("command") == "go test ./..."          # the WORKTREE's runner

    # CONTRAST (the binding is load-bearing, not always-worktree): _detect_runner
    # returns None for the construction root and the worktree's command for run.tool —
    # so binding to the construction repo would have yielded command=None here.
    from peers.spine.direction import _detect_runner
    assert _detect_runner(construction) is None               # construction root: NO runner
    assert _detect_runner(worktree) == "go test ./..."        # worktree: the bound runner


# ==========================================================================
# FLEET-E1 (edge) — malformed manifest fails closed at intake (exit 2); nothing
# spawned, no fleet ledger created.
# ==========================================================================
def test_fleet_e1_malformed_manifest_fails_closed(tmp_path):
    repoX = _repo(tmp_path, "repoX")
    # empty runs list -> load_fleet_manifest raises ValueError before any schedule.
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"pool": {"slots": ["s0"]}, "runs": []}))

    conducted = {"n": 0}

    def fake_conduct(*a, **k):           # must NEVER run on a bad manifest
        conducted["n"] += 1
        return None

    class _SR:
        def __init__(self):
            self.shut = False

        def shutdown(self):
            self.shut = True

    sr = _SR()
    rc = cmd_fleet(str(bad), ledger=tmp_path / "fleet.jsonl",
                   _conduct=fake_conduct, _make_slot_runner=lambda m: sr)
    assert rc == 2                                    # fail-closed intake
    assert conducted["n"] == 0                        # nothing scheduled
    assert not (tmp_path / "fleet.jsonl").exists()    # no ledger created

    # a second malformed shape (a run with no tool) also fails closed at intake.
    bad2 = tmp_path / "bad2.yaml"
    bad2.write_text(yaml.safe_dump(
        {"pool": {"slots": ["s0"]},
         "runs": [{"run_id": "a", "mode": "develop"}]}))   # missing 'tool'
    rc2 = cmd_fleet(str(bad2), ledger=tmp_path / "fleet2.jsonl",
                    _conduct=fake_conduct, _make_slot_runner=lambda m: _SR())
    assert rc2 == 2
    assert conducted["n"] == 0
    _ = repoX  # repo built to mirror the plan setup; intake fails before it matters


# ==========================================================================
# FLEET-E2 (edge) — a self-hosting converged run routes to Tier-2 (human review)
# and is NEVER auto-landed, even on an auto-merge landing decision.
# ==========================================================================
def test_fleet_e2_self_hosting_run_routes_tier2_never_lands(tmp_path, monkeypatch):
    from peers.spine.self_hosting import is_self_hosting as real_is_self_hosting

    repoX = _repo(tmp_path, "repoX")
    mpath = _single_develop_manifest(tmp_path, repoX, landing="auto-merge", max_ticks=6)

    # an injected lander that WOULD land anything (so a Tier-1 escape would show as a
    # landed run); the substrate self-hosting re-detect must keep it OUT of Tier-1.
    landed_calls = []

    def greedy_lander(run, *, repo, target_ref):
        landed_calls.append(run.mode_run)
        return LandingResult(landed=True, merged_sha="b" * 40, target_ref=target_ref)

    # REAL self-hosting detection on the converged diff: the fake builder's commit
    # touches src/peers/spine/*.py (a governance path), so is_self_hosting -> True
    # via the path-glob layer (target_repo is the leased worktree, not peers).
    def shost(repo, *, changed_paths, target_repo=None):
        # ignore target identity (a tmp repo is not peers); decide on the diff only,
        # so the test exercises the path-glob governance layer deterministically.
        return real_is_self_hosting(repo, changed_paths=changed_paths, target_repo=None)

    res, fl, _psr = _spawn_fleet(
        tmp_path, mpath, "tests.unit._active_fleet_builder_selfhost",
        monkeypatch=monkeypatch, is_self_hosting=shost, lander=greedy_lander,
        max_ticks=6)

    # HONESTY: the conductor re-checks self-hosting on the REAL converged diff and
    # routes devX to Tier-2; it is NEVER offered to the lander (no auto-land).
    assert "devX" in res.tier2, f"got tier2={res.tier2} cause={res.cause} st={res.statuses}"
    assert "devX" not in res.landed
    assert landed_calls == []                          # the lander was never invoked
    assert fl.latest_status("devX") != "landed"

    # negative-control: the converged diff really does touch the governance surface,
    # so is_self_hosting on it returns True (the substrate basis for the routing).
    ref = _git(repoX, "rev-parse", "refs/peers/fleet/devX").strip()
    rec = json.loads((repoX / ".peers" / "fleet-runs" / "devX" / "record.json").read_text())
    base = rec["base_sha"]
    from peers.spine.auto_merge import _changed_paths
    changed = _changed_paths(repoX, base, ref)
    assert any(p.startswith("src/peers/spine/") for p in (changed or []))
    assert real_is_self_hosting(repoX, changed_paths=changed, target_repo=None)[0] is True
