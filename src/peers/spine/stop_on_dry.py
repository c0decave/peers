"""STEP-5 — stop-on-dry: terminate a run after N dry rounds.

A run is *dry* when rounds go by without real, verifiable progress. The streak
counts consecutive rounds back to the most recent **real** confirmed unit; only a
real unit resets it. A real unit is a ``confirmed-work`` row with BOTH a
substrate ``author`` (attested — STEP-2) AND a ``witness``. This is load-bearing
(FIX 9): a bare or fabricated ``confirmed-work`` (no author, or no witness) is
counted as just another dry round, so an agent cannot defer stop-on-dry forever
by emitting a fake "confirmed".

Pure functions over ``LedgerEntry`` rows — no I/O. Non-round events (``run-start``,
``bar-inferred``, ``gate``, ``stop``) are ignored entirely; only ``dry-round`` and
``confirmed-work`` rows are rounds.
"""
from __future__ import annotations

from collections.abc import Sequence

from peers.spine.ledger import LedgerEntry

#: Events that constitute a "round" the stop-on-dry counter weighs. Everything
#: else (run-start, bar-inferred, gate, stop, …) is structural and ignored.
_ROUND_EVENTS = ("dry-round", "confirmed-work")


def _is_real_confirmed_work(row: LedgerEntry) -> bool:
    """A confirmed-work row counts as real progress (a reset) iff it carries a
    substrate-attested ``author`` AND a ``witness``."""
    return (
        row.event == "confirmed-work"
        and row.author is not None
        and row.witness is not None
    )


def dry_streak(rows: Sequence[LedgerEntry]) -> int:
    """Number of trailing dry rounds since the most recent real confirmed unit.

    Walk the rows from the end: a real confirmed-work stops the walk (it reset
    the streak); any other round row (a ``dry-round`` or a *fake* confirmed-work)
    increments the count; non-round events are skipped. Returns 0 for an empty
    ledger or when the last round was a real reset.
    """
    streak = 0
    for row in reversed(list(rows)):
        if _is_real_confirmed_work(row):
            break                       # real progress -> streak resets here
        if row.event in _ROUND_EVENTS:
            streak += 1                 # a dry round (incl. a fabricated confirm)
        # else: structural row -> ignored
    return streak


def should_stop(rows: Sequence[LedgerEntry], *, n: int) -> bool:
    """True when the dry streak has reached the threshold ``n``."""
    return dry_streak(rows) >= n
