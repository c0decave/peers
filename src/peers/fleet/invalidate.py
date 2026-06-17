"""STEP-5 -- the PURE transitive cascade revoke over recorded edges.

Dependents build on a producer's CONVERGED state BEFORE it lands (07 §7.4); so a
producer later REJECTED must transitively invalidate EVERY dependent that consumed
its artifact (07 §7.7). The CONDUCTOR (STEP-6) records each propagation EDGE into
the fleet-ledger when a consumer is scheduled (intra AND cross, per-producer on
fan-in -- the catastrophic gap the blockers name: ``propagate_branch`` records to
the consumer PER-RUN ledger, a different file; intra deps record nothing; so
without the conductor's edge recording the cascade walks an EMPTY set).

``cascade_invalidate`` here is the PURE graph walk over those LIVE recorded edges
-- idempotent + cycle-safe (a visited-set guard terminates on any pathological
cyclic edge set even though ``validate_program`` rejects cycles). It revokes off
the CURRENT edge set: ``FleetLedger.propagation_edges()`` already excludes
superseded edges, so a re-converged dependent is not over-revoked by an orphaned
edge from a prior repaired attempt (major F3-superseded). The CONDUCTOR consumes
the returned set to mark each revoked run ``rejected``, pull its landing from
Tier-1, reschedule it against the corrected artifact, and escalate an already-
``landed`` dependent to Tier-2 -- ``cascade_invalidate`` itself is the pure
topology computation only.
"""
from __future__ import annotations

from collections import deque


def dependents_of(fleet_ledger, run_id) -> set[str]:
    """The DIRECT (one-hop) dependents of ``run_id`` over the LIVE edges.

    ``{to for (frm, to, _art) in fleet_ledger.propagation_edges() if frm == run_id}``
    -- superseded edges are already excluded by ``propagation_edges()``.
    """
    return {
        to
        for frm, to, _art in fleet_ledger.propagation_edges()
        if frm == run_id
    }


def cascade_invalidate(fleet_ledger, rejected_run) -> set[str]:
    """The full forward transitive closure over the LIVE edges, EXCLUDING
    ``rejected_run`` itself.

    Pure function of the live recorded edges + the rejected id: idempotent
    (re-running yields the same set; an already-rejected node revokes nothing new),
    and cycle-safe (the ``seen`` visited-set makes it terminate on any cyclic edge
    set -- a defense-in-depth guard). A ``rejected_run`` that appears in no edge
    (or an empty edge set) yields the empty set, never a ``KeyError`` -- the
    conductor's reject path must fail SAFE on a fleet that has scheduled nothing.
    """
    edges = fleet_ledger.propagation_edges()
    adj: dict[str, set[str]] = {}
    for frm, to, _art in edges:
        adj.setdefault(frm, set()).add(to)
    revoked: set[str] = set()
    seen = {rejected_run}
    queue: deque[str] = deque([rejected_run])
    while queue:
        cur = queue.popleft()
        for dep in adj.get(cur, ()):     # direct dependents of cur
            if dep not in seen:          # visited-set guard => terminates on any cycle
                seen.add(dep)
                revoked.add(dep)
                queue.append(dep)
    return revoked                       # transitive closure, excluding rejected_run
