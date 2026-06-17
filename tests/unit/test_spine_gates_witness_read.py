"""BUG-719: the witness-ledgered gate must re-derive a file-kind witness with
the SAME hardened reader the producer uses (safe_io.read_bytes_no_symlink),
not a raw symlink-following / uncapped ``Path(uri).read_bytes()``.

The ``uri`` is taken from a ``confirmed-work`` row in the agent-writable
``.peers/run.jsonl`` (see gates.py docstring), so the verifier must fail
closed on a symlinked / non-regular / oversized ``uri`` rather than follow
it. Otherwise an agent satisfies the integrity gate by pointing ``uri`` at
a symlink (or, for the DoS variant, a multi-GB file).
"""
from __future__ import annotations

import hashlib
import os

import pytest

from peers.spine.gates import _witness_re_derives


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- happy: a real regular file whose content hashes to the claimed sha ------

def test_happy_regular_file_witness_re_derives(tmp_path):
    p = tmp_path / "evidence.txt"
    p.write_bytes(b"real artifact")
    witness = {"kind": "file", "uri": str(p), "sha256": _sha(b"real artifact")}
    assert _witness_re_derives(witness, tmp_path) is True


# --- edge: zero-length artifact (boundary) still re-derives -------------------

def test_edge_empty_file_witness_re_derives(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    witness = {"kind": "file", "uri": str(p), "sha256": _sha(b"")}
    assert _witness_re_derives(witness, tmp_path) is True


# --- sad: malicious / malformed uris must be refused, fail-closed ------------

def test_sad_symlinked_uri_is_refused(tmp_path):
    # The reproduction: target has the claimed content, but `uri` is a SYMLINK
    # to it. A symlink-following read would accept this (returns True); the
    # hardened no-follow read must refuse it.
    target = tmp_path / "target.txt"
    target.write_bytes(b"real artifact")
    link = tmp_path / "link.txt"
    os.symlink(target, link)
    witness = {"kind": "file", "uri": str(link), "sha256": _sha(b"real artifact")}
    assert _witness_re_derives(witness, tmp_path) is False


def test_sad_missing_uri_is_refused(tmp_path):
    witness = {"kind": "file", "uri": str(tmp_path / "nope.txt"),
               "sha256": _sha(b"whatever")}
    assert _witness_re_derives(witness, tmp_path) is False


def test_sad_non_regular_uri_is_refused(tmp_path):
    # A directory (non-regular) must be refused, not read.
    d = tmp_path / "adir"
    d.mkdir()
    witness = {"kind": "file", "uri": str(d), "sha256": _sha(b"")}
    assert _witness_re_derives(witness, tmp_path) is False


@pytest.mark.parametrize("bad", [None, "", 123, {"k": "v"}])
def test_sad_non_string_uri_is_refused(tmp_path, bad):
    witness = {"kind": "file", "uri": bad, "sha256": _sha(b"x")}
    assert _witness_re_derives(witness, tmp_path) is False
