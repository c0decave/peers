"""BUG-720: RunLedger.read() must signal a row missing required keys as a
CATCHABLE corruption (ValueError), not an uncatchable KeyError.

``.peers/run.jsonl`` is agent-writable, and ``spine.mode_run.drive()`` wraps
``ledger.read()`` in ``except (ValueError, OSError)`` to fail closed on a
corrupt/torn ledger. A syntactically valid JSON object that lacks
``event``/``status`` parses in ``_read_raw`` and then hits the bare
``d["event"]`` subscript — a ``KeyError`` that escapes the fail-closed
handler as an uncaught driver crash. read() must instead raise ValueError,
the same corruption class as the JSONDecodeError it already propagates.
"""
from __future__ import annotations

import json

import pytest

from peers.spine.ledger import RunLedger, _compute_entry_sha


def _write_hash_valid_row(path, **overrides):
    payload = {
        "v": 1,
        "prev": None,
        "event": "run-status",
        "mode_run": "run-a",
        "author": None,
        "subject": "run-a",
        "status": "running",
        "witness": {"slot": "s0"},
        "independence": False,
    }
    payload.update(overrides)
    payload["entry_sha"] = _compute_entry_sha(payload)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


# --- happy: a well-formed row reads back into a LedgerEntry ------------------

def test_happy_well_formed_row_reads(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="run-start", status="complete")
    rows = led.read()
    assert len(rows) == 1
    assert rows[0].event == "run-start"
    assert rows[0].status == "complete"


# --- edge: a row missing only OPTIONAL keys still reads (defaults apply) -----

def test_edge_row_missing_optional_keys_reads(tmp_path):
    p = tmp_path / "run.jsonl"
    # Only the two required keys present; author/witness/prev/etc. absent.
    p.write_text('{"event": "gate", "status": "pass"}\n', encoding="utf-8")
    rows = RunLedger(p).read()
    assert len(rows) == 1
    assert rows[0].event == "gate"
    assert rows[0].author is None
    assert rows[0].witness is None
    assert rows[0].v == 1


# --- happy: a hash-valid row with typed optional fields still verifies -------

def test_happy_hash_valid_typed_row_reads_and_verifies(tmp_path):
    p = tmp_path / "run.jsonl"
    _write_hash_valid_row(p, subject="run-b", witness={"slot": "s1"})
    led = RunLedger(p)
    rows = led.read()
    assert len(rows) == 1
    assert rows[0].event == "run-status"
    assert rows[0].status == "running"
    assert rows[0].subject == "run-b"
    assert rows[0].witness == {"slot": "s1"}
    assert led.verify() is True


# --- sad: a valid-JSON row missing a REQUIRED key is catchable corruption ---

def test_sad_row_missing_event_raises_catchable_valueerror(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text('{"status": "pass"}\n', encoding="utf-8")
    # drive() catches (ValueError, OSError) to fail closed; the error MUST be
    # in that set, not a KeyError that escapes the handler.
    with pytest.raises(ValueError):
        RunLedger(p).read()


def test_sad_row_missing_status_raises_catchable_valueerror(tmp_path):
    p = tmp_path / "run.jsonl"
    p.write_text('{"event": "gate"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        RunLedger(p).read()


def test_sad_corruption_is_catchable_by_drive_handler(tmp_path):
    # Mirrors spine.mode_run.drive()'s `except (ValueError, OSError)` posture.
    p = tmp_path / "run.jsonl"
    p.write_text('{"foo": 1}\n', encoding="utf-8")
    caught = False
    try:
        RunLedger(p).read()
    except (ValueError, OSError):
        caught = True
    assert caught, "missing-key row must be catchable as corruption"


# --- BUG-722: a valid-JSON NON-object row (42, "x", [..], null, true) is also -
# --- corruption. BUG-720 only covered objects-missing-keys; a non-object line -
# --- escaped via TypeError (read), AttributeError (verify) or AttributeError ---
# --- (append/_last_entry_sha). run.jsonl is agent-writable, so all three must --
# --- fail closed, not crash through the driver's fail-closed handler. ---------

_NON_OBJECT_ROWS = ("42", '"foo"', "[1, 2, 3]", "null", "true", "3.14")


@pytest.mark.parametrize("row", _NON_OBJECT_ROWS)
def test_sad_non_object_row_read_raises_catchable_valueerror(tmp_path, row):
    p = tmp_path / "run.jsonl"
    p.write_text(row + "\n", encoding="utf-8")
    # Must be ValueError (corruption), NOT TypeError, so drive()'s
    # `except (ValueError, OSError)` fail-closed handler catches it.
    with pytest.raises(ValueError):
        RunLedger(p).read()


@pytest.mark.parametrize("row", _NON_OBJECT_ROWS)
def test_sad_non_object_row_verify_fails_closed(tmp_path, row):
    # verify() is documented fail-closed: a corrupt ledger returns False, never
    # raises (an AttributeError here would escape its own except handler).
    p = tmp_path / "run.jsonl"
    p.write_text(row + "\n", encoding="utf-8")
    assert RunLedger(p).verify() is False


def test_sad_non_object_row_caught_by_drive_handler(tmp_path):
    # Same posture assertion as the BUG-720 sibling test, for non-object rows.
    p = tmp_path / "run.jsonl"
    p.write_text("42\n", encoding="utf-8")
    caught = False
    try:
        RunLedger(p).read()
    except (ValueError, OSError):
        caught = True
    assert caught, "non-object row must be catchable as corruption, not TypeError"


# --- edge: the WRITE path tolerates a non-object trailing line the same way it -
# --- already tolerates a torn (unparseable) line: skip it, chain onto the last -
# --- real entry, and keep appending. ------------------------------------------

def test_edge_append_after_non_object_last_line_chains_on_real_entry(tmp_path):
    p = tmp_path / "run.jsonl"
    led = RunLedger(p)
    first = led.append(event="run-start", status="complete")
    # An agent plants a valid-JSON non-object line as the new trailing line.
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("42\n")
    # The next substrate append must NOT crash; it skips the non-object line and
    # links its `prev` to the last REAL entry's sha (torn-line-tolerant posture).
    second = led.append(event="gate", status="pass")
    assert second.prev == first.entry_sha
    # read() still fails closed on the corrupt middle row, proving the skip is a
    # write-path leniency only -- the strict readers still surface the tamper.
    with pytest.raises(ValueError):
        led.read()


# --- happy: a normal object row is unaffected by the non-object guard ----------

def test_happy_object_row_unaffected_by_non_object_guard(tmp_path):
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="run-start", status="complete")
    rows = led.read()
    assert len(rows) == 1 and rows[0].event == "run-start"
    assert led.verify() is True


# --- BUG-727: hash-valid objects with wrong schema TYPES are corruption. -----
# A forged row can recompute entry_sha, so the hash chain alone cannot prove
# the dataclass contract that downstream fleet/spine consumers rely on.

_BAD_TYPED_FIELDS = (
    ("event", 123),
    ("status", ["running"]),
    ("subject", ["run-a"]),
)


@pytest.mark.parametrize(("field", "value"), _BAD_TYPED_FIELDS)
def test_sad_hash_valid_wrong_typed_row_read_raises_valueerror(tmp_path, field, value):
    p = tmp_path / "run.jsonl"
    _write_hash_valid_row(p, **{field: value})
    with pytest.raises(ValueError):
        RunLedger(p).read()


@pytest.mark.parametrize(("field", "value"), _BAD_TYPED_FIELDS)
def test_sad_hash_valid_wrong_typed_row_verify_fails_closed(tmp_path, field, value):
    p = tmp_path / "run.jsonl"
    _write_hash_valid_row(p, **{field: value})
    assert RunLedger(p).verify() is False


def test_sad_wrong_typed_subject_is_catchable_by_drive_handler(tmp_path):
    p = tmp_path / "run.jsonl"
    _write_hash_valid_row(p, subject=["run-a"])
    caught = False
    try:
        RunLedger(p).read()
    except (ValueError, OSError):
        caught = True
    assert caught, "wrong-typed subject must be catchable corruption, not TypeError"
