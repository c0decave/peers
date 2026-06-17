"""Stage-6 STEP-2 — the S2 auto-merge decision inside ``build_landing_contract``.

Stage 4 clamped ``landing_mode`` to ``branch-pr`` UNCONDITIONALLY. Stage 6
replaces that clamp with the precise §6.3 conjunction: a run lands ``auto-merge``
iff the operator requested it (``landing_mode == "auto-merge"``) AND the run is
mergeable (all spine gates pass AND ``resolves_to_commit(head)``) AND it is NOT
self-hosting. Any single false term → the fail-closed ``branch-pr`` default.

These tests pin the full decision matrix. ``build_landing_contract`` stays pure
and never-raises (a fabricated/None head records ``mergeable=False`` and short-
circuits to branch-pr rather than crashing).
"""
from tests.unit._baseline_landing_helpers import _run, _attested_repo
from peers.spine.landing import build_landing_contract
from peers.spine.gates import resolves_to_commit
from peers.spine.op_config import load_op_config


def _gated_ledger(tmp_path, sha):
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    run.ledger.append_attested(tmp_path, sha, event="confirmed-work", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True,
        mode_run="r1")
    run.ledger.append(event="stop", status="complete", mode_run="r1")
    return run.ledger.read()


def _gate_failing_ledger(tmp_path):
    # run-start + stop but NO confirmed-work row: `witness-ledgered` fails (nothing
    # witnessed => no self-green) while ModeRun-valid + stop-on-dry pass, so
    # all_pass(gates) is False purely because a spine GATE failed -- distinct from
    # the fabricated/None-head route exercised by the not-mergeable head tests.
    run = _run(tmp_path)
    load_op_config(run.op_config, run.ledger, mode_run="r1")
    run.ledger.append(event="stop", status="complete", mode_run="r1")
    return run.ledger.read()


def test_auto_merge_when_requested_mergeable_and_not_self_hosting(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="auto-merge", self_hosting=False)
    assert lc.mergeable is True and lc.landing_mode == "auto-merge"   # the new affordance


def test_self_hosting_forces_branch_pr_even_when_mergeable(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="auto-merge", self_hosting=True)
    assert lc.landing_mode == "branch-pr" and lc.self_hosting is True   # §6.3


def test_not_mergeable_forces_branch_pr_even_if_requested(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha="deadbeef", landing_mode="auto-merge",  # head fabricated
                                self_hosting=False)
    assert lc.mergeable is False and lc.landing_mode == "branch-pr"


def test_failing_spine_gate_forces_branch_pr_even_with_valid_head(tmp_path):
    # BUG-602 / S2 matrix (the 6th case): auto-merge REQUESTED with a VALID
    # resolvable head, but a spine GATE fails (no confirmed-work => witness-ledgered
    # False => all_pass False). mergeable is False via the GATE route (not the
    # fabricated-head route of test_not_mergeable_forces_branch_pr_even_if_requested),
    # so the run still lands branch-pr. The explicit gate assertion proves this
    # exercises the gate path and would not pass vacuously.
    sha = _attested_repo(tmp_path, "claude")
    assert resolves_to_commit(tmp_path, sha) is True          # the head IS valid...
    rows = _gate_failing_ledger(tmp_path)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="auto-merge", self_hosting=False)
    assert lc.gates["witness-ledgered"] is False              # ...but a spine gate failed
    assert lc.mergeable is False and lc.landing_mode == "branch-pr"


def test_branch_pr_request_stays_branch_pr(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="branch-pr", self_hosting=False)
    assert lc.landing_mode == "branch-pr"


def test_unknown_landing_mode_defaults_branch_pr(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="merge-now",  # not the exact token
                                self_hosting=False)
    assert lc.landing_mode == "branch-pr"     # default-deny: only "auto-merge" enables it


def test_none_head_with_auto_merge_request_does_not_raise(tmp_path):
    # edge / S2: a None head over an otherwise-passing ledger records mergeable=False
    # and short-circuits to branch-pr WITHOUT raising (the "never raises" guarantee
    # the Stage-4 clamp owned must survive the S2 swap).
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=None, landing_mode="auto-merge", self_hosting=False)
    assert lc.mergeable is False and lc.landing_mode == "branch-pr"


def test_empty_landing_mode_defaults_branch_pr(tmp_path):
    # edge / default-deny: an empty-string landing token is NOT the exact "auto-merge"
    # token, so it lands branch-pr even when mergeable and not self-hosting.
    sha = _attested_repo(tmp_path, "claude")
    rows = _gated_ledger(tmp_path, sha)
    lc = build_landing_contract(rows, repo=tmp_path, mode_run="r1", branch="feat/x",
                                head_sha=sha, landing_mode="", self_hosting=False)
    assert lc.landing_mode == "branch-pr"
