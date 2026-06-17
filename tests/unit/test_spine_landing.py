from tests.unit._baseline_landing_helpers import _run, _attested_repo, _repo_with_commit
from peers.spine.landing import build_landing_contract
from peers.spine.gates import evaluate_spine_gates, all_pass

def _gated_ledger(tmp_path, sha):
    """Build a ledger whose four spine gates ALL pass: run-start + an attested,
    git-sha-witnessed confirmed-work + a terminal stop (no dry streak)."""
    run = _run(tmp_path)   # writes the op-config run-start lazily via load_op_config below
    from peers.spine.op_config import load_op_config
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    run.ledger.append_attested(tmp_path, sha, event="confirmed-work", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True,
        mode_run="r1")
    run.ledger.append(event="stop", status="complete", mode_run="r1")
    return run.ledger.read()

def test_landing_contract_mergeable_when_all_gates_pass(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    assert all_pass(evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)) is True
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1",
                                branch="feat/x", head_sha=sha)
    assert lc.mergeable is True and lc.landing_mode == "branch-pr"
    assert lc.gates["witness-ledgered"] is True and lc.head_sha == sha
    assert lc.branch == "feat/x" and lc.self_hosting is False

def test_landing_contract_not_mergeable_on_fabricated_head(tmp_path):
    # Every gate passes over the ledger, but the supplied head does NOT resolve ->
    # the contract must NOT assert it mergeable (records=does-not-assert).
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1",
                                branch="feat/x", head_sha="deadbeef")
    assert lc.mergeable is False and lc.head_sha == "deadbeef"

def test_landing_contract_not_mergeable_when_a_gate_fails(tmp_path):
    # An UNATTESTED real commit: authorship-attested gate is False -> not mergeable.
    sha = _repo_with_commit(tmp_path)
    from peers.spine.op_config import load_op_config
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    run.ledger.append_attested(tmp_path, sha, event="confirmed-work", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True,
        mode_run="r1")
    run.ledger.append(event="stop", status="complete", mode_run="r1")
    rows = run.ledger.read()
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1",
                                branch="feat/x", head_sha=sha)
    assert lc.gates["authorship-attested"] is False and lc.mergeable is False

def test_self_hosting_forces_branch_pr(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="auto-merge",  # an op tries to relax
                                self_hosting=True)
    assert lc.landing_mode == "branch-pr"      # self-hosting overrides any relaxation
    assert lc.self_hosting is True

def test_auto_merge_enabled_when_mergeable_and_not_self_hosting(tmp_path):
    # Stage 6 (§6.3) REMOVES the Stage-4 unconditional clamp: an op-config carrying
    # landing="auto-merge" over a mergeable, NOT-self-hosting run now lands auto-merge.
    # (The Stage-4 name asserted the dead clamp behaviour; this is the new affordance.)
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="auto-merge", self_hosting=False)
    assert lc.landing_mode == "auto-merge"     # the trusted-case affordance
    assert lc.self_hosting is False
