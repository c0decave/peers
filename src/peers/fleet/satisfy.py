"""STEP-3 — dependency satisfaction by RE-VERIFICATION, never trust.

A fleet dependency is satisfied ONLY by re-deriving the producer's CONVERGED
state from its OWN substrate (the fleet adds no new trust — 07 §7.7); a recorded
fleet-ledger ``status: converged`` row is NEVER sufficient.

  - intra-tool: ``is_converged`` over the PRODUCER's own ledger (the spine gates
    re-run + an attested confirmed-work row).
  - cross-tool: CONVERGED **and** the consumer-owned ``refs/propagated/<from>``
    pins the producer's converged tip (the recorded PROPAGATED edge, set ONLY by
    ``propagate_branch``) **and** producer-peer (resolved in ``producer_repo``)
    ≠ consumer-peer (resolved in ``consumer_repo`` over a tip RE-DERIVED from the
    CONSUMER's OWN ledger). Independence is RE-DERIVED from each repo's substrate
    ``refs/notes/peers-attest`` — NEVER the propagation row's agent-writable
    ``independence`` field, NEVER an unbound caller-supplied consumer tip.

Why (the Stage-5 carry-forward, load-bearing): ``.peers/`` is agent-writable, so
the propagation row's ``independence``/``author`` fields are not a trust anchor
(hash-chain ≠ authorship). The only trustworthy authorship signal is the
substrate notes ref read via ``resolve_author`` in the repo that owns it. Binding
the consumer tip to the consumer's OWN ledger (not a caller value) stops a single
agent authoring both ends from pointing the consumer-side ``resolve_author`` at a
real-but-irrelevant other-peer commit to fake distinctness. A same-peer cross-run
handoff (the cross-run form of self-greening) is rejected at the boundary; an
unattested either end fails CLOSED.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from peers.spine.authorship import resolve_author
from peers.spine.propagate import _converged_commit, is_converged


def _propagated_tip(consumer_ws, from_run) -> str | None:
    """The 40-hex sha ``refs/propagated/<from_run>`` resolves to in the consumer
    worktree, or ``None`` (fail-closed on any non-zero git rc / empty output).

    This consumer-owned ref is set ONLY by ``propagate_branch`` — it is the
    DISCRIMINATING PROPAGATED proof (Stage-5: ``resolves_to_commit`` is
    non-discriminating on the shared object DB, so mere reachability is not a
    consumption witness)."""
    proc = subprocess.run(
        ["git", "-C", str(consumer_ws.worktree_path), "rev-parse",
         "--verify", "--quiet", f"refs/propagated/{from_run}"],
        capture_output=True, text=True, timeout=120,    # match spine propagate._git
    )
    if proc.returncode != 0:
        return None
    tip = proc.stdout.strip()
    return tip or None


def _derived_consumer_tip(consumer_run) -> str | None:
    """Re-derive the consumer's CONVERGED commit from the consumer's OWN ledger
    exactly as the producer side is derived (blocker F2-1 — the consumer tip is
    SUBSTRATE-BOUND, never a caller value).

    A consumer that is not CONVERGED on its own ledger cannot be a handoff
    endpoint -> ``None``."""
    rows = consumer_run.ledger.read()
    if is_converged(rows, mode_run=consumer_run.mode_run, repo=consumer_run.tool,
                    head=getattr(consumer_run, "branch", None) or "HEAD"):
        return _converged_commit(rows)
    return None


def dep_satisfied(producer_run, consumer_run, *, producer_repo, consumer_repo,
                  kind, consumer_ws=None) -> bool:
    """True iff the dependency ``producer_run -> consumer_run`` is satisfied by
    RE-VERIFICATION. ``kind`` is ``"intra"`` (same tool) or ``"cross"``
    (different tools); cross-tool spans TWO repos (distinct ODBs + notes refs),
    so the consumer workspace (``consumer_ws``) carrying ``refs/propagated/*`` is
    REQUIRED. ``consumer_run`` is used: its OWN ledger re-derives the consumer
    tip (never a caller value)."""
    # BUG-300 (fix_by claude): kind is a 2-valued discriminator. The production
    # make_dep_check only ever derives "intra"/"cross", but a DIRECT caller typo
    # must NOT silently fall through to the cross-tool branch -- an unrecognized
    # kind is a caller contract violation, rejected LOUDLY (fail-closed: a typo can
    # produce NEITHER a satisfied verdict NOR a quietly-wrong False).
    if kind not in ("intra", "cross"):
        raise ValueError(
            f"unknown dep kind {kind!r}: expected 'intra' or 'cross'"
        )
    rows = producer_run.ledger.read()
    # F2: ALWAYS re-derive CONVERGED from the PRODUCER's own ledger via the spine
    # gates -- never a fleet-ledger status row. Resolved in the PRODUCER repo.
    if not is_converged(rows, mode_run=producer_run.mode_run, repo=producer_repo,
                        head=getattr(producer_run, "branch", None) or "HEAD"):
        return False
    if kind == "intra":
        return True                          # converged producer + intra-tool -> satisfied
    # kind == "cross": CONVERGED + a recorded PROPAGATED edge + substrate-re-derived
    # producer-peer != consumer-peer, with EACH side resolved in ITS OWN repo.
    if consumer_ws is None:
        return False                         # cross-tool needs the consumer workspace
    producer_tip = _converged_commit(rows)
    if producer_tip is None:
        return False
    propagated = _propagated_tip(consumer_ws, producer_run.mode_run)
    if propagated is None or propagated.lower() != producer_tip.lower():
        return False                         # not propagated (or to a different tip)
    # blocker F2-1: the consumer tip is RE-DERIVED from the CONSUMER's own substrate,
    # never a caller value the agent could redirect at a decoy other-peer commit.
    consumer_tip = _derived_consumer_tip(consumer_run)
    if consumer_tip is None:
        return False                         # consumer not converged on its own ledger
    # major F2-2 + carry-forward: RE-DERIVE each peer in ITS OWN repo (distinct notes
    # refs); a single repo can resolve only one side -> fail closed on an unknown tip.
    producer_peer = resolve_author(producer_repo, producer_tip)
    consumer_peer = resolve_author(consumer_repo, consumer_tip)
    if producer_peer is None or consumer_peer is None:
        return False                         # unattested either end -> fail closed
    if producer_peer == consumer_peer:
        return False                         # same peer -> cross-run self-green (rejected)
    return True


def make_dep_check(specs_by_id, runs_by_id, repos_by_id) -> Callable[[str, str], bool]:
    """The PRODUCTION ``dep_check`` adapter the scheduler/conductor inject.

    Returns a closure ``dep_check(producer_id, consumer_id) -> bool`` that binds
    ``kind``/repos/the consumer workspace ENTIRELY from substrate:
      - ``kind`` is DERIVED from tool identity (``Path(...).resolve()`` equality
        of the two specs' tool roots) — never a client opt-in;
      - the consumer workspace is DERIVED from the consumer run's own worktree +
        branch (so ``refs/propagated/*`` is read where the consumer actually ran);
      - the verdict is ``dep_satisfied``'s pure per-run re-verification.

    It draws NOTHING from a fleet-ledger row — it has no fleet-ledger handle at
    all, so a row claiming ``converged``/``independent`` cannot influence it."""
    def dep_check(producer_id: str, consumer_id: str) -> bool:
        producer_spec = specs_by_id[producer_id]
        consumer_spec = specs_by_id[consumer_id]
        producer_run = runs_by_id[producer_id]
        consumer_run = runs_by_id[consumer_id]
        kind = ("intra"
                if Path(producer_spec.tool).resolve() == Path(consumer_spec.tool).resolve()
                else "cross")
        # derive the consumer ws from the consumer run's OWN worktree/branch --
        # _propagated_tip only needs worktree_path; branch is carried for parity.
        consumer_ws = SimpleNamespace(worktree_path=Path(consumer_run.tool),
                                      branch=consumer_run.branch)
        return dep_satisfied(producer_run, consumer_run,
                             producer_repo=repos_by_id[producer_id],
                             consumer_repo=repos_by_id[consumer_id],
                             kind=kind, consumer_ws=consumer_ws)
    return dep_check
