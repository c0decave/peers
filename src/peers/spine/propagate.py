"""STEP-4 — the CONVERGED-gated, attested propagation seam (Stage 5).

PROPAGATION is the EXPLICIT step by which a dependent run consumes ONLY a
producer's CONVERGED artifact — never the producer's live in-progress worktree.
It is DISTINCT from ``landing`` (human-merge delivery): a dependent builds on
the PROPAGATED-CONVERGED branch tip, not the landed-to-mainline state.

:func:`is_converged` decides CONVERGED ([07 §7.4]): the producer ledger passes
all four spine gates AND carries ≥1 attested+witnessed ``confirmed-work`` row.
:func:`propagate_branch` refuses a non-converged producer (writing NOTHING into
the consumer); for a converged one it makes the tip reachable in the consumer
worktree and pins it in a CONSUMER-OWNED ``refs/propagated/<from_run>`` ref
(``fetch`` + ``update-ref`` — NEVER ``branch -f``, which fails rc=128 when the
producer holds its branch checked out), then records the edge on the CONSUMER
ledger via ``append_attested`` (the producer's substrate-attested author — no
forged cross-run handoff; never re-attests).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.spine.gates import all_pass, evaluate_spine_gates
from peers.spine.ledger import RunLedger
from peers.spine.worktree import PropagationResult


def is_converged(rows, *, mode_run, repo, head="HEAD") -> bool:
    """True iff ``rows`` is a CONVERGED run: every spine gate passes AND there is
    ≥1 ``confirmed-work`` row claiming ``independence`` — whose author the
    ``authorship-attested`` gate RE-DERIVES from the substrate ``peers-attest``
    note (full-depth-analysis §1), rather than trusting the agent-writable row
    ``author`` field directly. (CONVERGED per [07 §7.4] — passes all gates +
    carries attested INDEPENDENT confirmed work — NOT landed-to-main.)

    ``head`` is the run's tip (its branch, or its pinned ref post-teardown) that
    the authorship gate anchors attest-commit reachability on (HONEST-01): a
    ``peers-attest`` note minted on a dangling/out-of-branch commit is rejected.
    Defaults to ``HEAD`` (fail-closed for a branch run whose caller omits it)."""
    gates = evaluate_spine_gates(rows, mode_run=mode_run, repo=repo, head=head)
    return all_pass(gates) and any(
        r.event == "confirmed-work" and r.independence for r in rows)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120, check=False)


def _converged_commit(rows) -> str | None:
    """The attested commit of the latest git-sha-witnessed ``confirmed-work`` row
    — the CONVERGED branch artifact. ``None`` when no confirmed-work row carries a
    git-sha witness (e.g. a file-witnessed research run has no propagatable branch
    commit). This is the sha attested AT convergence — deliberately NOT the live
    ``rev-parse {branch}`` tip, which a live producer may have advanced past
    convergence to an un-attested commit (REVIEW-A: the gate validates the ledger,
    so the shipped sha must be bound to it, not to the mutable branch head)."""
    for r in reversed(rows):
        if (r.event == "confirmed-work" and isinstance(r.witness, dict)
                and r.witness.get("kind") == "git-sha"):
            sha = r.witness.get("sha256") or r.witness.get("uri")
            if isinstance(sha, str) and len(sha) == 40:
                return sha.lower()
    return None


def propagate_branch(producer_run, consumer_ws, *, repo) -> PropagationResult:
    """Publish the producer's CONVERGED branch tip into ``consumer_ws``.

    Refuses unless ``producer_run`` is converged (``reason="not-converged"``,
    nothing written). On success: ``fetch`` the producer branch into the
    consumer worktree (explicit object reachability) + ``update-ref`` the
    consumer-owned ``refs/propagated/<from_run>`` to the tip (NEVER ``branch -f``
    — rc=128 when the producer holds the branch checked out), then record the
    edge on the CONSUMER ledger via ``append_attested(repo, tip, ...)`` (the
    producer's attested author — never re-attested). Returns a
    :class:`PropagationResult` whose ``git-sha`` witness RECORDS which converged
    tip transferred (the Stage-7 fleet-ledger edge)."""
    rows = producer_run.ledger.read()
    if not is_converged(rows, mode_run=producer_run.mode_run, repo=repo,
                        head=getattr(producer_run, "branch", None) or "HEAD"):
        return PropagationResult(ok=False, reason="not-converged")

    # Ship the CONVERGED artifact — the attested confirmed-work commit recorded in
    # the producer ledger — NOT the live `rev-parse {branch}` tip. is_converged
    # validated the ledger (which pins the attested commit); a live producer may
    # have advanced the branch past convergence to an un-attested commit, so the
    # shipped sha is bound to the ledger, never to the mutable branch head (REVIEW-A).
    converged = _converged_commit(rows)
    if converged is None or len(converged) != 40:
        return PropagationResult(ok=False, reason="no-artifact")

    # Defense in depth (REVIEW-B): the shipped commit MUST itself be substrate-
    # attested. is_converged only requires SOME confirmed-work author; a witness
    # sha forged to diverge from that author would otherwise be moved here and
    # written as an independence=True / author=None row that PERMANENTLY poisons
    # the consumer's authorship-attested gate (the ledger is append-only).
    from peers.spine.authorship import resolve_author
    author = resolve_author(repo, converged)
    if author is None:
        return PropagationResult(ok=False, reason="unattested-tip")

    consumer = consumer_ws.worktree_path
    # (a) explicit object reachability into the consumer (the ODB is shared, but
    #     the fetch is the explicit grant); (b) pin the CONVERGED commit in a
    #     CONSUMER-OWNED namespaced ref the producer never checks out. NEVER
    #     `branch -f` the producer branch (rc=128 when it holds the branch checked out).
    fetch = _git(consumer, "fetch", str(repo), producer_run.branch)
    if fetch.returncode != 0:
        return PropagationResult(ok=False, reason="move-failed")
    upd = _git(consumer, "update-ref", f"refs/propagated/{producer_run.mode_run}", converged)
    if upd.returncode != 0:
        return PropagationResult(ok=False, reason="move-failed")

    witness = {"kind": "git-sha", "uri": converged, "sha256": converged,
               "from_run": producer_run.mode_run, "to_run": consumer_ws.mode_run,
               "artifact": producer_run.branch}
    # record the edge on the CONSUMER ledger; append_attested re-derives the author
    # of `converged` = the PRODUCER's attested peer (the note is in the shared
    # peers-attest ref, visible from `repo`) — never re-attested by the consumer.
    # `independence` is computed from the attested author (non-None by the guard
    # above) as a SECOND layer: a future change that drops the guard still cannot
    # write an unattested independence row (REVIEW-B defense in depth).
    consumer_ledger = RunLedger(Path(consumer) / ".peers" / "run.jsonl")
    consumer_ledger.append_attested(
        repo, converged, event="propagation", subject=producer_run.branch, status="ok",
        witness=witness, independence=author is not None, mode_run=consumer_ws.mode_run)

    return PropagationResult(ok=True, witness=witness, artifact=producer_run.branch)
