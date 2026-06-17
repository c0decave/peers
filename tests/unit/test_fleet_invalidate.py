# tests/unit/test_fleet_invalidate.py
"""STEP-5 -- the PURE transitive cascade revoke over recorded edges.

The first block is the canonical fail-first contract from
docs/plans/2026-06-11-agentic-os-stage-7.md (STEP-5): transitive-completeness on a
linear chain, a diamond fanned-in once, a leaf revoking nothing, idempotence,
direct-vs-transitive ``dependents_of``, cycle termination (visited-set guard), and
the superseded-edge exclusion (major F3-superseded).

The second block adds happy / edge / sad coverage the canonical names do not
exercise (mirroring the STEP-4 scheduler convention) -- and hunts adjacent bugs
the contract list omits: a cascade over a fleet-ledger with NO edges at all, and a
``rejected_run`` that is UNKNOWN to the edge set (a caller id that appears in no
edge). Both must fail SAFE (empty set, no KeyError), never crash the conductor.

Host-only + deterministic: REAL tmp git repos (via _isolation_helpers), a real
attested edge per (from, to) -- NO network, NO LLM, NO containers.
"""
from tests.unit._fleet_helpers import _fleet_ledger
from tests.unit._isolation_helpers import _attested_repo, _commit_on_branch

from peers.fleet.invalidate import cascade_invalidate, dependents_of


def _edge(fl, repo, frm, to):
    # a real attested edge (the substrate author is recorded) -- mirrors STEP-2.
    tip = _commit_on_branch(
        repo, f"peers/run/{frm}-{to}", f"{frm}_{to}.py", "x", peer="claude"
    )
    fl.record_propagation_edge(frm, to, f"peers/run/{frm}", repo=repo, tip_sha=tip)


# ----------------------------------------------------------------------------
# canonical fail-first contract (STEP-5): transitive completeness + safety
# ----------------------------------------------------------------------------
def test_linear_chain_revokes_transitively_happy_path(tmp_path):
    # A<-B<-C : reject A revokes B AND C (the transitive-completeness proof).
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    _edge(fl, tmp_path, "b", "c")
    revoked = cascade_invalidate(fl, "a")
    assert revoked == {"b", "c"}                        # both, transitively


def test_diamond_revokes_each_dependent_once(tmp_path):
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    for frm, to in [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]:
        _edge(fl, tmp_path, frm, to)
    revoked = cascade_invalidate(fl, "a")
    assert revoked == {"b", "c", "d"}                   # d reached via both paths, once


def test_leaf_rejection_revokes_empty_set_edge(tmp_path):
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    assert cascade_invalidate(fl, "b") == set()         # b has no dependents


def test_cascade_is_idempotent(tmp_path):
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    _edge(fl, tmp_path, "b", "c")
    once = cascade_invalidate(fl, "a")
    # mark them rejected (the conductor's job) then re-run -> same closure, no error
    for rid in once:
        fl.record_status(rid, "rejected")
    twice = cascade_invalidate(fl, "a")
    assert once == twice == {"b", "c"}


def test_dependents_of_returns_direct_one_hop_only(tmp_path):
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    _edge(fl, tmp_path, "b", "c")
    assert dependents_of(fl, "a") == {"b"}              # DIRECT dependents only (one hop)


def test_cycle_in_edges_terminates(tmp_path):
    # defense in depth: even a (pathological) cyclic edge set must not loop forever.
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    _edge(fl, tmp_path, "b", "a")                       # a<->b (should never happen post-validate)
    revoked = cascade_invalidate(fl, "a")
    assert "b" in revoked                               # terminates (visited-set guard)


def test_cascade_ignores_superseded_edges(tmp_path):
    # major F3-superseded: A rejected, B revoked, A corrected, B re-converges against
    # the new artifact (a NEW edge). The OLD A->B edge is marked superseded. Rejecting
    # an UNRELATED run must NOT pull B via the stale edge.
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")                       # the first (pre-repair) A->B edge
    # the conductor supersedes the stale edge on a successful repair-reconverge:
    fl.supersede_edge("a", "b")                         # appends a `revoked`/superseded marker
    _edge(fl, tmp_path, "a2", "b")                      # B re-converged against A's corrected run
    # rejecting an unrelated run z must not drag B via the dead a->b edge
    assert cascade_invalidate(fl, "z") == set()
    # and the dead edge is gone from the live set
    assert ("a", "b", "peers/run/a") not in fl.propagation_edges()


def test_dependents_of_excludes_superseded_edge(tmp_path):
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    fl.supersede_edge("a", "b")
    assert dependents_of(fl, "a") == set()              # the superseded edge is not live


# ----------------------------------------------------------------------------
# adjacent-bug hunt (not in the canonical list): degenerate edge sets fail SAFE
# ----------------------------------------------------------------------------
def test_cascade_over_empty_edge_set_revokes_nothing_edge(tmp_path):
    # boundary: a fleet-ledger with NO propagation edges at all. cascade_invalidate
    # must return the empty set (not KeyError on a missing/empty ledger) -- otherwise
    # the conductor's reject path crashes on a fleet that has scheduled nothing yet.
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    assert fl.propagation_edges() == []                 # precondition: empty edge set
    assert cascade_invalidate(fl, "a") == set()
    assert dependents_of(fl, "a") == set()


def test_cascade_on_unknown_rejected_run_returns_empty_sad(tmp_path):
    # sad: the rejected id appears in NO edge (an unknown / already-pruned run-id).
    # The forward closure of a node not in the graph is empty -- never an error.
    _attested_repo(tmp_path)
    fl = _fleet_ledger(tmp_path)
    _edge(fl, tmp_path, "a", "b")
    _edge(fl, tmp_path, "b", "c")
    assert cascade_invalidate(fl, "zzz-not-a-run") == set()
    assert dependents_of(fl, "zzz-not-a-run") == set()
