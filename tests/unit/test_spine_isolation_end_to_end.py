import subprocess
from tests.unit._isolation_helpers import (_git, _attested_repo, _commit_on_branch, _run)

from peers.spine.worktree import (GitWorktreeProvider, workspace_names,
                                  propagatable_artifacts)
from peers.spine.propagate import propagate_branch
from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config


def _converged_ledger(repo, ledger_path, mode_run, tip):
    led = RunLedger(ledger_path)
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run=mode_run)
    led.append_attested(repo, tip, event="confirmed-work", subject="F1", status="pass",
                        witness={"kind": "git-sha", "uri": tip, "sha256": tip},
                        independence=True, mode_run=mode_run)
    led.append(event="stop", status="complete", mode_run=mode_run)
    return led


def test_two_runs_progress_without_clobbering_each_others_state(tmp_path):
    _attested_repo(tmp_path)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "r1") as a, prov.lease(tmp_path, "r2") as b:
        # each run mutates its OWN worktree + commits on its OWN branch
        (a.worktree_path / "a.txt").write_text("A")
        _git(a.worktree_path, "add", "a.txt")
        _git(a.worktree_path, "commit", "-q", "-m", "A")
        (b.worktree_path / "b.txt").write_text("B")
        _git(b.worktree_path, "add", "b.txt")
        _git(b.worktree_path, "commit", "-q", "-m", "B")
        # neither run sees the other's working state (no shared writable HEAD)
        assert not (a.worktree_path / "b.txt").exists()
        assert not (b.worktree_path / "a.txt").exists()
        # each has its OWN .peers/ run.lock (own fault domain)
        assert (a.worktree_path / ".peers" / "run.lock").exists()
        assert (b.worktree_path / ".peers" / "run.lock").exists()
        pa, pb = str(a.worktree_path), str(b.worktree_path)
    # both teardown clean -- no leftover worktrees (paths live OUTSIDE the repo
    # under a `peers-run-*` mkdtemp root, so check the actual leased paths)
    out = _git(tmp_path, "worktree", "list", "--porcelain")
    assert pa not in out and pb not in out
    assert "peers/run/r1" not in out and "peers/run/r2" not in out


def _propagated_ref(ws, from_run):
    r = subprocess.run(["git", "-C", str(ws.worktree_path), "rev-parse",
                        "--verify", "--quiet", f"refs/propagated/{from_run}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def test_dependent_consumes_only_propagated_converged_artifact(tmp_path):
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix", peer="claude")
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    producer._ledger = _converged_ledger(tmp_path, tmp_path / "p.jsonl", "p1", tip)
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        # THE REAL BARRIER (working-tree isolation): the producer's committed file
        # is NOT materialized in the consumer's working tree (it sits on the
        # producer branch, not the consumer's HEAD).
        assert not (consumer_ws.worktree_path / "fix.py").exists()
        # discriminating NEGATIVE: the consumer-owned propagated ref does NOT exist
        # before propagate (resolves_to_commit(consumer, tip) is ALREADY True here
        # via the shared ODB -- it does NOT discriminate, so we assert the ref).
        assert _propagated_ref(consumer_ws, "p1") is None
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is True
        # discriminating POSITIVE: propagate set the consumer-owned ref to the
        # producer's converged tip -- the recorded fleet-ledger edge of WHICH tip
        # was transferred (the git-sha witness records, it does not prove isolation).
        assert _propagated_ref(consumer_ws, "p1") == tip
        edge = RunLedger(consumer_ws.worktree_path / ".peers" / "run.jsonl").read()[-1]
        assert edge.event == "propagation" and edge.witness["from_run"] == "p1"
        assert edge.witness["sha256"] == tip


def test_non_converged_producer_propagates_nothing(tmp_path):
    _attested_repo(tmp_path)
    _commit_on_branch(tmp_path, "peers/run/p1", "fix.py", "fix")
    producer = _run(tmp_path, mode_run="p1", branch="peers/run/p1")
    led = RunLedger(tmp_path / "p.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="p1")
    led.append(event="dry-round", status="dry", mode_run="p1")     # NOT converged
    producer._ledger = led
    prov = GitWorktreeProvider()
    with prov.lease(tmp_path, "c1") as consumer_ws:
        res = propagate_branch(producer, consumer_ws, repo=tmp_path)
        assert res.ok is False and res.reason == "not-converged"
        assert not (consumer_ws.worktree_path / "fix.py").exists()  # nothing leaked


def test_stage7_namer_non_colliding_and_artifact_set(tmp_path):
    # the §7.2 validator inputs: a pure non-colliding namer + the declared set.
    w1, b1 = workspace_names(tmp_path, "r1")
    w2, b2 = workspace_names(tmp_path, "r2")
    assert w1 != w2 and b1 != b2                          # provably non-colliding by name
    run = _run(tmp_path, mode_run="r1", branch="peers/run/r1")
    assert propagatable_artifacts(run) == ["peers/run/r1"]   # what the producer CAN emit
