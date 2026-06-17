"""STEP-5 — stop-on-dry counter over ledger rows.

A run terminates after `n` consecutive *dry* rounds. The streak resets ONLY on a
**real** confirmed unit — one with a substrate `author` AND a `witness`. A bare
or fabricated `confirmed-work` (no author, or no witness) does NOT reset: it
counts as just another dry round, so an agent cannot defer stop-on-dry forever by
printing a fake "confirmed" (decision FIX 9). `dry_streak` is a pure function
over `LedgerEntry` rows — non-round events (run-start, bar-inferred, gate, stop)
are ignored.

Covers happy (real confirmed-work resets), edge (empty; reset at the tail; only
non-round rows), sad (fabricated confirmed-work — no author, or witness-without-
author — does not reset).
"""
from peers.spine.ledger import LedgerEntry
from peers.spine.stop_on_dry import dry_streak, should_stop


def _row(event, **kw):
    base = dict(v=1, prev=None, mode_run=None, author=None, subject=None,
                status="dry", witness=None, independence=False, entry_sha="x")
    base.update(event=event, **kw)
    return LedgerEntry(**base)


def test_real_confirmed_work_resets_streak():
    rows = [
        _row("dry-round", status="dry"),
        _row("confirmed-work", status="pass", author="claude",
             witness={"kind": "exit-code", "uri": "pytest", "sha256": "abc"}),  # real -> resets
        _row("dry-round", status="dry"),
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 2
    assert should_stop(rows, n=2) is True
    assert should_stop(rows, n=3) is False


def test_fabricated_confirmed_work_does_not_reset():
    rows = [
        _row("dry-round", status="dry"),
        _row("confirmed-work", status="pass", author=None, witness=None),  # fake -> no reset
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 3        # all three rounds count; the fake didn't reset


def test_witness_without_author_does_not_reset():
    # sad: a witness alone is not enough; an unattested confirmed-work is fake.
    rows = [
        _row("dry-round", status="dry"),
        _row("confirmed-work", status="pass", author=None,
             witness={"kind": "file", "uri": "/x", "sha256": "abc"}),
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 3


def test_author_without_witness_does_not_reset():
    # sad: an attested author with no witness is also not a real unit.
    rows = [
        _row("confirmed-work", status="pass", author="claude", witness=None),
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 2        # both rows count; no reset happened


def test_empty_rows_has_zero_streak():
    assert dry_streak([]) == 0
    assert should_stop([], n=1) is False


def test_reset_at_tail_is_zero_streak():
    # edge: a real confirmed-work as the LAST row -> nothing after it -> 0.
    rows = [
        _row("dry-round", status="dry"),
        _row("dry-round", status="dry"),
        _row("confirmed-work", status="pass", author="claude",
             witness={"kind": "exit-code", "uri": "pytest", "sha256": "z"}),
    ]
    assert dry_streak(rows) == 0
    assert should_stop(rows, n=1) is False


def test_non_round_events_are_ignored():
    # edge: run-start / bar-inferred / gate must not inflate the streak.
    rows = [
        _row("run-start", status="ok"),
        _row("bar-inferred", status="pass"),
        _row("dry-round", status="dry"),
        _row("gate", status="pass"),
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 2        # only the two dry-round rows count
    assert should_stop(rows, n=2) is True


def test_real_reset_clears_earlier_fakes():
    # a later real unit resets even though earlier fakes existed.
    rows = [
        _row("confirmed-work", status="pass", author=None, witness=None),  # fake
        _row("confirmed-work", status="pass", author="claude",
             witness={"kind": "exit-code", "uri": "pytest", "sha256": "q"}),  # real reset
        _row("dry-round", status="dry"),
    ]
    assert dry_streak(rows) == 1
