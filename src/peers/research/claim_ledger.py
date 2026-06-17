"""STEP-1 — the origin-independent claim classifier.

The §5.2 honesty rule, made into a pure predicate: a load-bearing claim is
``confirmed`` only when it is backed by **≥2 origin-independent witnesses**.
Independence is by *resolved origin* — two witness URLs that resolve to the
same canonical origin count as ONE witness (so a single source echoed across
two pages does not self-confirm). This source-origin axis is **orthogonal** to
the spine's ``independence`` flag (PEER/author independence, the
no-self-greening axis) and lives entirely here in ``src/peers/research/`` —
it must never overload the spine flag.

``classify_claim`` is sticky on an explicit ``contested`` status: a claim a
verifier marked contested stays contested even with two distinct origins (a
contradiction is not resolved by counting corroborations).
"""
from __future__ import annotations

from collections.abc import Iterable

from peers.research.ports import Claim, Witness

#: Claim status constants (also the values the ledger ``claim`` row records).
CONFIRMED = "confirmed"
SINGLE_SOURCE = "single-source"
CONTESTED = "contested"
UNVERIFIED_GAP = "unverified-gap"


def independent_origins(witnesses: Iterable[Witness]) -> int:
    """Count distinct resolved origins among ``witnesses``.

    Two witnesses with the same ``resolved_origin`` count as one — the
    load-bearing 5.2 rule that prevents a single source echoed across two URLs
    from self-confirming a claim.

    A **falsy** ``resolved_origin`` (empty string — a fetch that could not
    resolve an origin) is NOT counted: an origin-less witness corroborates
    nothing, so it must not push a claim over the ≥2-origin confirmation
    threshold (BUG-527 — tighten-only defense in depth on the 5.2 anti-
    self-confirmation rule).
    """
    return len({o for w in witnesses if (o := w.resolved_origin)})


def classify_claim(claim: Claim) -> str:
    """Classify a load-bearing claim by its origin-independent witness count.

    - an explicit ``contested`` status is preserved (sticky — a contradiction
      is not cleared by corroboration count);
    - ≥2 origin-independent witnesses → ``confirmed``;
    - exactly 1 → ``single-source``;
    - 0 → ``unverified-gap``.

    Only a ``confirmed`` claim is eligible to become a ``confirmed-work`` unit
    upstream; ``single-source`` / ``contested`` / ``unverified-gap`` are routed
    to the report's gaps (a dry round).
    """
    if claim.status == CONTESTED:
        return CONTESTED
    n = independent_origins(claim.witnesses)
    if n >= 2:
        return CONFIRMED
    if n == 1:
        return SINGLE_SOURCE
    return UNVERIFIED_GAP
