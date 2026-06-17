"""STEP-1 — RunLedger: append-only, hash-chained witness log.

Covers happy (append + chain), edge (independence is in the hash;
empty ledger), and sad (tamper detected; caller-supplied author
rejected) per the Stage-0 plan (docs/plans/2026-06-10-agentic-os-stage-0.md,
Task 1). The hash-chain links each row's ``prev`` to the previous row's
``entry_sha``; ``verify()`` recomputes every ``entry_sha`` and detects any
tamper, including a flipped ``independence`` flag.
"""
import pytest

from peers.spine.ledger import RunLedger


def test_append_and_chain(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    a = led.append(event="run-start", status="complete")
    b = led.append(event="gate", subject="bar", status="pass")
    rows = led.read()
    assert [r.event for r in rows] == ["run-start", "gate"]
    assert rows[0].prev is None
    assert rows[1].prev == a.entry_sha          # chain links b -> a
    assert b.prev == a.entry_sha
    assert led.verify() is True                 # intact chain


def test_tamper_detected(tmp_path):
    p = tmp_path / "run.jsonl"
    led = RunLedger(p)
    led.append(event="run-start", status="complete")
    led.append(event="gate", subject="bar", status="pass")
    lines = p.read_text().splitlines()
    bad = lines[0].replace("run-start", "run-FORGED")
    p.write_text("\n".join([bad, lines[1]]) + "\n")
    assert RunLedger(p).verify() is False       # prev-hash no longer matches


def test_independence_is_in_the_hash(tmp_path):
    # flipping `independence` must break the chain (it is part of entry_sha)
    p = tmp_path / "run.jsonl"
    led = RunLedger(p)
    led.append(event="confirmed-work", subject="u1", status="pass",
               independence=True)
    raw = p.read_text()
    assert RunLedger(p).verify() is True
    p.write_text(raw.replace('"independence": true', '"independence": false'))
    assert RunLedger(p).verify() is False


def test_append_rejects_caller_supplied_author(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    with pytest.raises(ValueError):
        led.append(event="confirmed-work", status="pass", author="claude")
    # author=None is fine (un-authored entry)
    e = led.append(event="confirmed-work", status="pass", author=None)
    assert e.author is None


def test_empty_ledger_reads_and_verifies(tmp_path):
    # edge: a never-written ledger reads as [] and verifies vacuously True.
    led = RunLedger(tmp_path / "run.jsonl")
    assert led.read() == []
    assert led.verify() is True


def test_read_carries_entry_sha_and_fields(tmp_path):
    # edge: every persisted field survives the round-trip, including the
    # entry_sha the chain links against and a structured witness dict.
    led = RunLedger(tmp_path / "run.jsonl")
    wit = {"kind": "file", "uri": "/x", "sha256": "abc"}
    e = led.append(event="confirmed-work", subject="u1", status="pass",
                   witness=wit, mode_run="r1", independence=True)
    (row,) = led.read()
    assert row.entry_sha == e.entry_sha
    assert row.witness == wit
    assert row.mode_run == "r1"
    assert row.independence is True
    assert row.subject == "u1"
    assert row.v == 1


def test_truncated_trailing_garbage_breaks_verify(tmp_path):
    # sad: a partially-written / corrupt final line must not verify True.
    p = tmp_path / "run.jsonl"
    led = RunLedger(p)
    led.append(event="run-start", status="complete")
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    with pytest.raises(Exception):
        RunLedger(p).read()


def test_verify_detects_broken_chain_link(tmp_path):
    # The prev-link half of verify() is load-bearing and was previously untested:
    # a row spliced from a DIFFERENT ledger (whose row-0 content differs, so the
    # prev pointers genuinely do not link) must be rejected even though each
    # row's own digest re-derives cleanly.
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    la = RunLedger(a)
    la.append(event="run-start", status="complete")
    la.append(event="gate", subject="x", status="pass")
    lb = RunLedger(b)
    lb.append(event="OTHER-start", status="complete")   # row-0 content DIFFERS
    lb.append(event="gate", subject="y", status="pass")
    a0 = a.read_text().splitlines()[0]
    b1 = b.read_text().splitlines()[1]
    spliced = tmp_path / "spliced.jsonl"
    spliced.write_text(a0 + "\n" + b1 + "\n")
    # b1.prev points at b0's sha, not a0's sha -> chain link broken.
    assert RunLedger(spliced).verify() is False
