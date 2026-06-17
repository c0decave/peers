"""STEP-6 — reusable N-vote adversarial-verify gate.

A claim is checked by EXACTLY K independent refuters; it survives iff fewer than
a majority refute it (`refuted < ceil(k/2)`). Uncertainty defaults to refuted —
a refuter that raises, or returns anything other than an explicit `False`, counts
as a refutation (fail-closed). The verdict is recorded as a `gate` ledger row.
The K-count is pinned: all K refuters are consulted even once the outcome is
decided (FIX 10).

Covers happy (minority refute survives), edge (k=1; even-k tie kills; ledger
optional), sad (majority/all refute kills; a raising/None refuter counts as
refuted; k<1 rejected).
"""
import pytest

from peers.spine.adversarial_verify import verify_claim
from peers.spine.ledger import RunLedger


def test_majority_refute_kills(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    votes = iter([True, True, False])  # refuted, refuted, not
    survived = verify_claim("c1", refuter=lambda i: next(votes), k=3, ledger=led,
                            mode_run="r1")
    assert survived is False
    assert any(r.event == "gate" and r.subject == "c1" for r in led.read())


def test_minority_refute_survives(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    votes = iter([True, False, False])
    assert verify_claim("c1", refuter=lambda i: next(votes), k=3, ledger=led,
                        mode_run="r1") is True


def test_k_vote_count_is_pinned(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    calls = []
    survived = verify_claim("c1", refuter=lambda i: (calls.append(i) or True), k=5,
                            ledger=led, mode_run="r1")
    assert len(calls) == 5            # exactly K refuters were consulted
    assert sorted(calls) == [0, 1, 2, 3, 4]
    assert survived is False          # 5 refutes >= ceil(5/2)=3 -> killed


def test_k1_single_refuter(tmp_path):
    # edge: k=1 -> ceil(1/2)=1 -> survive iff refuted == 0.
    led = RunLedger(tmp_path / "run.jsonl")
    assert verify_claim("c1", refuter=lambda i: False, k=1, ledger=led,
                        mode_run="r1") is True
    assert verify_claim("c2", refuter=lambda i: True, k=1, ledger=led,
                        mode_run="r1") is False


def test_even_k_tie_kills(tmp_path):
    # edge: k=4 -> ceil(4/2)=2 -> exactly-2 refutes (a tie) kills; 1 survives.
    led = RunLedger(tmp_path / "run.jsonl")
    tie = iter([True, True, False, False])
    assert verify_claim("c1", refuter=lambda i: next(tie), k=4, ledger=led,
                        mode_run="r1") is False
    one = iter([True, False, False, False])
    assert verify_claim("c2", refuter=lambda i: next(one), k=4, ledger=led,
                        mode_run="r1") is True


def test_raising_refuter_counts_as_refuted(tmp_path):
    # sad: an erroring refuter is maximal uncertainty -> refuted (fail-closed).
    led = RunLedger(tmp_path / "run.jsonl")

    def boom(i):
        raise RuntimeError("verifier crashed")

    assert verify_claim("c1", refuter=boom, k=3, ledger=led, mode_run="r1") is False


def test_none_vote_counts_as_refuted(tmp_path):
    # sad: only an explicit False clears; None (don't-know) counts as refuted.
    led = RunLedger(tmp_path / "run.jsonl")
    assert verify_claim("c1", refuter=lambda i: None, k=3, ledger=led,
                        mode_run="r1") is False


def test_invalid_k_is_rejected(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    with pytest.raises(ValueError):
        verify_claim("c1", refuter=lambda i: False, k=0, ledger=led, mode_run="r1")
    with pytest.raises(ValueError):
        verify_claim("c1", refuter=lambda i: False, k=-2, ledger=led, mode_run="r1")


def test_ledger_optional_and_verdict_recorded(tmp_path):
    # ledger is optional (reusable off-ledger); when given, the gate row records
    # status + the vote tally in the witness.
    assert verify_claim("c1", refuter=lambda i: False, k=3) is True   # no ledger
    led = RunLedger(tmp_path / "run.jsonl")
    verify_claim("c1", refuter=lambda i: True, k=3, ledger=led, mode_run="r1")
    (row,) = [r for r in led.read() if r.event == "gate"]
    assert row.status == "fail" and row.subject == "c1"
    assert row.witness["k"] == 3 and row.witness["refuted"] == 3
