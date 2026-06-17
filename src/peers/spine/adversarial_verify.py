"""STEP-6 — a reusable N-vote adversarial-verify gate.

A claim survives only if it withstands EXACTLY K independent refutation attempts.
Each refuter is *trying to refute* the claim; the claim survives iff fewer than a
majority succeed (`refuted < ceil(k/2)`). Two load-bearing properties:

1. **Fail-closed / default-to-refuted.** Only an explicit ``False`` from a refuter
   counts as "could not refute". Anything else — ``True``, ``None``, a non-bool,
   or an exception — counts as a refutation. Uncertainty never helps the claim.
2. **K is pinned (FIX 10).** All K refuters are consulted even after the verdict
   is mathematically decided, so the audit trail always shows K independent votes.

The refuter is an injected callable ``refuter(i) -> bool`` so the gate is
loop-agnostic and testable; the modes wire real refuters (Workflow/agent votes)
behind it later. The verdict is recorded as a ``gate`` ledger row.
"""
from __future__ import annotations

from collections.abc import Callable

from peers.spine.ledger import RunLedger


def _majority_threshold(k: int) -> int:
    """ceil(k / 2) — the number of refutations that kills the claim."""
    return (k + 1) // 2


def verify_claim(
    subject: str,
    *,
    refuter: Callable[[int], bool],
    k: int,
    ledger: RunLedger | None = None,
    mode_run: str | None = None,
) -> bool:
    """Run ``k`` independent refuters against ``subject``; return whether it
    survives (``refuted < ceil(k/2)``).

    ``refuter(i)`` is called for every ``i in range(k)`` (no short-circuit). A
    call that returns anything other than ``False`` — or raises — is counted as a
    refutation (fail-closed). When ``ledger`` is given, a ``gate`` row records the
    verdict and the vote tally.
    """
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"k must be an int >= 1 (got {k!r})")

    refuted = 0
    for i in range(k):
        try:
            vote = refuter(i)
        except Exception:
            vote = True                 # erroring refuter -> maximal uncertainty
        if vote is not False:           # only an explicit False clears the claim
            refuted += 1

    threshold = _majority_threshold(k)
    survived = refuted < threshold

    if ledger is not None:
        ledger.append(
            event="gate",
            subject=subject,
            status="pass" if survived else "fail",
            mode_run=mode_run,
            witness={
                "kind": "adversarial-verify",
                "k": k,
                "refuted": refuted,
                "threshold": threshold,
            },
        )
    return survived
