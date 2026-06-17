import json
from tests.unit._baseline_landing_helpers import _run, _attested_repo
from peers.spine.landing import build_landing_contract
from peers.spine.gates import evaluate_spine_gates
from peers.spine.op_config import load_op_config

def _gated_ledger(tmp_path, sha):
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    run.ledger.append_attested(tmp_path, sha, event="confirmed-work", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True, mode_run="r1")
    run.ledger.append(event="stop", status="complete", mode_run="r1")
    return run

def test_to_witness_is_url_kind_carrying_the_structured_contract(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    run = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(run.ledger.read(), repo=tmp_path, mode_run="r1",
                                branch="feat/x", head_sha=sha)
    w = lc.to_witness()
    assert w["kind"] == "url" and w["uri"] == "feat/x" and w["landing"] == "branch-pr"
    assert w["contract"]["mergeable"] is True
    assert w["contract"]["gates"]["witness-ledgered"] is True
    assert w["contract"]["head_sha"] == sha and w["contract"]["self_hosting"] is False
    assert w["contract"]["landing_mode"] == "branch-pr"   # carried INSIDE the contract dict
    json.dumps(w)            # JSON round-trips -> stable ledger digest

def test_landing_record_does_not_self_green_the_gates(tmp_path):
    # Appending a landing row with the contract witness must NOT, by itself, make
    # witness-ledgered pass — it is a record (kind 'url'), not a confirmed-work witness.
    sha = _attested_repo(tmp_path, "claude")
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    lc = build_landing_contract(run.ledger.read(), repo=tmp_path, mode_run="r1",
                                branch="feat/x", head_sha=sha)
    run.ledger.append(event="landing", status="ok", subject="feat/x",
                      witness=lc.to_witness(), mode_run="r1")
    rows = run.ledger.read()
    # no confirmed-work row exists yet -> witness-ledgered stays False despite the landing row.
    assert evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)["witness-ledgered"] is False
