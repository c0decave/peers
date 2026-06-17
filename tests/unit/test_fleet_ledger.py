# tests/unit/test_fleet_ledger.py
"""STEP-2 contract tests for the hash-chained fleet-ledger.

Host-only + deterministic: real tmp git repos (via _isolation_helpers) for the
attested-edge cases; a plain tmp JSONL for the pure status/intent/halt cases.
Covers happy (append-only last-wins, attested edge, open intent), edge (dedup,
superseded, malformed-row escalation), and sad (unattested tip => author None,
tamper breaks verify) paths.
"""
import pytest

from tests.unit._fleet_helpers import _fleet_ledger
from tests.unit._isolation_helpers import _git, _attested_repo, _commit_on_branch

from peers.spine.authorship import resolve_author


def test_status_is_append_only_last_wins(tmp_path):
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "pending")
    fl.record_status("a", "running", slot="s0")
    fl.record_status("a", "converged")
    assert fl.latest_status("a") == "converged"
    assert fl.latest_status("ghost") is None
    assert fl.verify() is True                          # the chain still verifies


def test_slot_assignment_recorded(tmp_path):
    fl = _fleet_ledger(tmp_path)
    fl.record_slot("a", "s1")
    assert fl.slot_of("a") == "s1"


def test_propagation_edge_is_attested(tmp_path):
    # the edge author is the SUBSTRATE peer of the tip (append_attested), never a
    # caller claim -- this is what F2's independence re-derivation reads.
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix", peer="claude")
    fl = _fleet_ledger(tmp_path)
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=tmp_path, tip_sha=tip)
    edges = fl.propagation_edges()
    assert ("a", "b", "peers/run/a") in edges
    # the recorded edge row carries the attested author of the tip; independence is
    # COMPUTED (author is not None), NEVER a literal True (blocker F3-edge-2).
    rows = [r for r in fl.read() if r.event == "propagation-edge"]
    assert rows and rows[-1].author == resolve_author(tmp_path, tip) == "claude"
    assert rows[-1].independence is True                # because author is non-None


def test_cross_tool_edge_carries_attested_author_not_none(tmp_path):
    # blocker F3-edge-2: a cross-tool edge is recorded against the repo where the tip
    # is ACTUALLY attested -- so the author resolves (never None / independence=True
    # with author=None, the append-only authorship poison REVIEW-B forbids).
    _attested_repo(tmp_path)                            # the producer's repo (notes here)
    tip = _commit_on_branch(tmp_path, "peers/run/p", "p.py", "p", peer="claude")
    fl = _fleet_ledger(tmp_path)
    fl.record_propagation_edge("p", "c", "peers/run/p", repo=tmp_path, tip_sha=tip)
    rows = [r for r in fl.read() if r.event == "propagation-edge"]
    assert rows[-1].author == "claude"                  # NOT None
    # and a row whose tip is UNATTESTED records author=None AND independence=False
    # (the computed flag tracks the absent attestation -- no poison row).
    _git(tmp_path, "checkout", "-q", "-b", "peers/run/u")
    (tmp_path / "u.py").write_text("u")
    _git(tmp_path, "add", "u.py")
    _git(tmp_path, "commit", "-q", "-m", "u")
    unattested = _git(tmp_path, "rev-parse", "HEAD").strip()
    fl.record_propagation_edge("u", "c", "peers/run/u", repo=tmp_path, tip_sha=unattested)
    last = [r for r in fl.read() if r.event == "propagation-edge"][-1]
    assert last.author is None and last.independence is False


def test_propagation_edge_is_deduplicated(tmp_path):
    # the same (from, to) recorded twice yields ONE edge in propagation_edges() so the
    # cascade never double-counts (the conductor records per-(producer,consumer) pair).
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix", peer="claude")
    fl = _fleet_ledger(tmp_path)
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=tmp_path, tip_sha=tip)
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=tmp_path, tip_sha=tip)
    assert fl.propagation_edges().count(("a", "b", "peers/run/a")) == 1


def test_malformed_edge_row_does_not_silently_drop_invalidation(tmp_path):
    # minor F3-edge-3: a propagation-edge row whose witness is missing to_run must NOT
    # raise KeyError (which fail-OPENS the whole cascade) -- it is skipped + recorded as
    # a malformed-edge marker (an undeterminable dependency -> escalate, never silent).
    fl = _fleet_ledger(tmp_path)
    # write a torn edge row directly (a future schema variant / partial write)
    fl._led.append(event="propagation-edge", status="ok", subject="a->",
                   witness={"from_run": "a", "artifact": "x"})   # NO to_run
    edges = fl.propagation_edges()                       # must NOT raise
    assert edges == []                                   # the malformed row yields no edge
    assert any(r.event == "malformed-edge" for r in fl.read())   # but it IS flagged


@pytest.mark.parametrize("bad_witness", [["from_run", "a"], "from_run=a", 42, 3.5])
def test_non_dict_witness_edge_row_does_not_fail_open(tmp_path, bad_witness):
    # F3-edge-3 (fail-CLOSED, regression): a propagation-edge row whose witness is a
    # TRUTHY NON-DICT JSON value (a list/str/number -- the `.peers/` ledger is
    # agent-writable, so a torn/forged row of any shape is in-threat-model) must NOT
    # raise AttributeError out of propagation_edges(). An unguarded `w.get(...)` over a
    # non-dict fail-OPENS the whole F3 cascade (a rejected producer's transitive
    # dependents are never revoked -> cross-run self-greening) AND crashes the conductor
    # step-0 halt-check. It must be skipped + flagged malformed-edge, exactly like a torn
    # dict row. (`slot_of`/`intents`/the malformed-set already guard with isinstance; this
    # pins the propagation-edge parse to the same rule.)
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="propagation-edge", status="ok", subject="a->b",
                   witness=bad_witness)                  # NON-dict witness
    edges = fl.propagation_edges()                       # must NOT raise
    assert edges == []                                   # the non-dict row yields no edge
    assert any(r.event == "malformed-edge" for r in fl.read())   # but it IS flagged


def test_non_dict_witness_superseded_row_does_not_raise(tmp_path):
    # F3-edge-3 sibling: a non-dict witness on an edge-superseded row must also not raise.
    # A non-dict superseded row cannot supersede anything -> the original edge stays LIVE,
    # which is fail-CLOSED for the cascade (the edge is still walked + revoked).
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="propagation-edge", status="ok", subject="a->b",
                   witness={"from_run": "a", "to_run": "b", "artifact": "git-sha"})
    fl._led.append(event="edge-superseded", status="ok", subject="a->b",
                   witness="not-a-dict")                 # NON-dict witness
    edges = fl.propagation_edges()                       # must NOT raise
    assert ("a", "b", "git-sha") in edges                # edge stays LIVE (non-dict can't supersede)


def test_start_intent_without_running_is_an_open_intent(tmp_path):
    # write-ahead: an intent with no subsequent running status is OPEN (the
    # crash-between-intent-and-start case the conductor reconciles).
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    assert ("a", "s0") in fl.intents()
    # blocker F5-2: an OPEN intent IS visible to latest_status/slot_of (the scheduler
    # must count its slot busy + project its cost across ticks).
    assert fl.latest_status("a") == "start-intent"
    assert fl.slot_of("a") == "s0"
    fl.record_status("a", "running", slot="s0")
    assert ("a", "s0") not in fl.intents()              # intent fulfilled -> no longer open


def test_intent_then_rejected_closes_the_intent(tmp_path):
    # minor F5-intents: a terminal status (rejected) after an intent closes it too.
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "rejected")
    assert ("a", "s0") not in fl.intents()


def test_halt_is_recorded(tmp_path):
    fl = _fleet_ledger(tmp_path)
    fl.record_halt("world-divergence: slot s0 runs unknown z")
    halts = [r for r in fl.read() if r.event == "halt"]
    assert halts and "divergence" in halts[-1].subject


def test_tamper_breaks_verify(tmp_path):
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "converged")
    # flip a byte in the JSONL -> the hash-chain verify() must fail
    p = fl.path
    text = p.read_text().replace("converged", "REJECTED")
    p.write_text(text)
    assert fl.verify() is False


# ---- Adjacent-bug hunt beyond the contract's verbatim bodies ----------------

def test_superseded_edge_is_excluded_then_relive_on_rerecord(tmp_path):
    # major F3-superseded: an edge superseded AFTER it was recorded drops out of the
    # live set; a FRESH record after the supersede brings it back live (the conductor
    # records a fresh edge on a repair-reconverge). Last-record-vs-last-supersede wins.
    _attested_repo(tmp_path)
    tip = _commit_on_branch(tmp_path, "peers/run/a", "fix.py", "fix", peer="claude")
    fl = _fleet_ledger(tmp_path)
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=tmp_path, tip_sha=tip)
    assert ("a", "b", "peers/run/a") in fl.propagation_edges()
    fl.supersede_edge("a", "b")
    assert fl.propagation_edges() == []                  # superseded -> not live
    # a NEW edge record after the supersede is live again (recorded later than supersede)
    tip2 = _commit_on_branch(tmp_path, "peers/run/a", "fix2.py", "fix2", peer="claude")
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=tmp_path, tip_sha=tip2)
    assert ("a", "b", "peers/run/a") in fl.propagation_edges()


def test_malformed_edge_marker_recorded_once_across_repeated_reads(tmp_path):
    # idempotency of the fail-closed parse: re-reading propagation_edges() must NOT
    # append a second malformed-edge marker for the same torn row (else the ledger
    # grows unboundedly each tick the conductor reads it).
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="propagation-edge", status="ok", subject="a->",
                   witness={"from_run": "a", "artifact": "x"})   # NO to_run
    fl.propagation_edges()
    fl.propagation_edges()
    fl.propagation_edges()
    markers = [r for r in fl.read() if r.event == "malformed-edge"]
    assert len(markers) == 1                             # flagged exactly once


def test_latest_status_ignores_other_runs(tmp_path):
    # edge: latest_status/slot_of are per-run_id -- interleaved rows for OTHER runs
    # must not bleed into a run's reported status/slot.
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")
    fl.record_status("b", "converged", slot="s1")
    fl.record_status("a", "converged")
    assert fl.latest_status("a") == "converged"
    assert fl.latest_status("b") == "converged"
    assert fl.slot_of("a") == "s0"                       # a's last slot witness
    assert fl.slot_of("b") == "s1"
    assert fl.slot_of("ghost") is None
