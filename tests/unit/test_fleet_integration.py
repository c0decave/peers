"""Stage-7 fleet DAEMON — integration capstone (TDD).

Closes the live-path correctness gap: a fleet child's per-run ledger + converged
commit MUST survive worktree teardown (else the conductor can't re-verify or
land). Proves (1) run_one persists the ledger + a stable commit ref before the
lease tears down, (2) the PEERS_FLEET_BUILDERS env hook wires a mode into a fresh
child, and (3) the FULL stack — real ProcessSlotRunner spawning a real run_one
child + conduct_fleet — drives a real (no-LLM) develop run to converge + land.
"""
from __future__ import annotations

import json
import os
import time

from peers.fleet import run_one
from peers.fleet.daemon import conduct_fleet
from peers.fleet.scheduler import Ceiling
from peers.fleet.slot_runner import ProcessSlotRunner
from peers.spine.auto_merge import LandingResult
from peers.spine.ledger import RunLedger
from peers.spine.propagate import _converged_commit, is_converged
from peers.spine.worktree import GitWorktreeProvider
from tests.unit._fleet_builder_dev import _GitCommitConverge
from tests.unit._isolation_helpers import _attested_repo, _git
from tests.unit.test_fleet_conductor import _converged_run  # noqa: F401 (parity import)


def _repo(tmp_path, name="x"):
    p = tmp_path / name
    p.mkdir()
    _attested_repo(p)
    return p


def _spec_json(repo, run_id="devA", *, branch=None):
    head = _git(repo, "rev-parse", "HEAD").strip()
    return json.dumps({
        "run_id": run_id, "tool": str(repo), "mode": "develop",
        "op_config": {"mode": "develop", "budget": {"max_rounds": 1}},
        "base_sha": head, "branch": branch or f"peers/run/{run_id}"})


# ---- (1) persistence -----------------------------------------------------
def test_ledger_and_commit_persist_after_worktree_teardown(tmp_path):
    x = _repo(tmp_path)
    rc = run_one.main(["--spec", _spec_json(x)],
                      factory=lambda s: _GitCommitConverge(),
                      provider=GitWorktreeProvider())
    assert rc == 0
    stable = x / ".peers" / "fleet-runs" / "devA" / "run.jsonl"
    assert stable.exists()                                  # ledger copied out
    rows = RunLedger(stable).read()
    # HONEST-01: re-verify anchored on the persisted ref (the run's branch is
    # torn down); the attest commit must be reachable from the pinned real tip.
    assert is_converged(rows, mode_run="devA", repo=x,
                        head="refs/peers/fleet/devA")      # re-verifiable post-teardown
    sha = _converged_commit(rows)
    ref = _git(x, "rev-parse", "refs/peers/fleet/devA").strip()
    assert ref == sha                                       # commit kept reachable


# ---- (2) env-builder hook ------------------------------------------------
def test_env_builders_register_a_mode(tmp_path):
    os.environ.pop("PEERS_FLEET_BUILDERS", None)
    try:
        run_one._FRONTEND_BUILDERS.pop("develop", None)
        run_one._load_env_builders("tests.unit._fleet_builder_dev")
        fe = run_one.default_factory({"mode": "develop"})
        assert isinstance(fe, _GitCommitConverge)
    finally:
        run_one._FRONTEND_BUILDERS.pop("develop", None)


def test_env_builders_bad_module_is_fail_closed(tmp_path):
    # a missing builder module must NOT register the mode -> it stays fail-closed.
    run_one._load_env_builders("tests.unit._no_such_builder_xyz")
    import pytest
    with pytest.raises(run_one.UnsupportedFleetMode):
        run_one.default_factory({"mode": "develop"})


# ---- (3) full-stack capstone (real subprocess) ---------------------------
def test_real_fleet_spawns_child_converges_and_lands(tmp_path, monkeypatch):
    x = _repo(tmp_path)
    # the spawned run_one child must import `tests.unit._fleet_builder_dev`, so the
    # worktree ROOT (where the `tests` package lives) must be on its PYTHONPATH.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    monkeypatch.setenv("PYTHONPATH",
                       repo_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("PEERS_FLEET_BUILDERS", "tests.unit._fleet_builder_dev")

    from peers.fleet.manifest import load_fleet_manifest
    m = load_fleet_manifest({
        "pool": {"slots": ["s0"]},
        "daemon": {"max_ticks": 120, "tick_sleep_s": 1, "idle_timeout_s": 120},
        "runs": [{"run_id": "devA", "tool": str(x), "mode": "develop",
                  "landing": "auto-merge", "budget": {"max_rounds": 1}}],
    })
    fl_path = tmp_path / "fleet.jsonl"
    from peers.fleet.fleet_ledger import FleetLedger
    fl = FleetLedger(fl_path)
    psr = ProcessSlotRunner(m.pool, m.repos_by_id, idle_timeout_s=120)
    lands = []

    def lander(run, *, repo, target_ref):
        lands.append(run.mode_run)
        return LandingResult(landed=True, merged_sha="b" * 40, target_ref=target_ref)

    try:
        res = conduct_fleet(
            fl, m.program, m.pool, Ceiling(), slot_runner=psr,
            repos_by_id=m.repos_by_id, dep_check=lambda p, c: True,
            is_self_hosting=lambda r, **kw: (False, ""), lander=lander,
            target_ref="main", max_ticks=120, tick_sleep_s=0.2, sleep=time.sleep)
    finally:
        psr.shutdown()

    assert res.cause == "complete", f"got {res.cause}: {res.statuses} err={res.error}"
    assert fl.latest_status("devA") == "landed"
    assert lands == ["devA"] and res.ok is True
