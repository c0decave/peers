"""STEP-2 — substrate-attested authorship on ledger entries.

``author`` is resolved ONLY from the ``refs/notes/peers-attest`` substrate
attestation note of a commit SHA — never from caller content. ``append_attested``
is the one sanctioned path that sets a non-None author; the public ``append``
still rejects a caller-supplied author (STEP-1 invariant).

Covers happy (attested SHA -> name), edge (unattested SHA -> None; the attested
entry still chains + verifies), and sad (bogus SHA -> None; an attested author
is still tamper-evident) per the Stage-0 plan (Task 2).
"""
import subprocess

import pytest

from peers import attest
from peers.spine.authorship import resolve_author
from peers.spine.ledger import RunLedger


def _git(p, *a):
    return subprocess.run(
        ["git", "-C", str(p), *a], capture_output=True, text=True, check=True,
    ).stdout


def _init(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")


def _commit(p, name):
    (p / name).write_text(name)
    _git(p, "add", name)
    _git(p, "commit", "-q", "-m", name)
    return _git(p, "rev-parse", "HEAD").strip()


def test_author_is_substrate_attested_not_caller_supplied(tmp_path):
    _init(tmp_path)
    base = _commit(tmp_path, "a.py")
    sha = _commit(tmp_path, "b.py")
    attest.attest_commits(tmp_path, "claude", base, sha)   # substrate stamps it
    assert resolve_author(tmp_path, sha) == "claude"


def test_unattested_commit_has_no_author(tmp_path):
    _init(tmp_path)
    sha = _commit(tmp_path, "a.py")
    assert resolve_author(tmp_path, sha) is None            # forgeable identity ignored


def test_append_attested_sets_substrate_author(tmp_path):
    _init(tmp_path)
    base = _commit(tmp_path, "a.py")
    sha = _commit(tmp_path, "b.py")
    attest.attest_commits(tmp_path, "codex", base, sha)
    led = RunLedger(tmp_path / "run.jsonl")
    e = led.append_attested(
        tmp_path, sha, event="confirmed-work", subject="u1", status="pass",
        witness={"kind": "git-sha", "uri": sha, "sha256": sha}, independence=True,
    )
    assert e.author == "codex"          # set by the substrate, not the caller


def test_resolve_author_on_nonexistent_sha_is_none(tmp_path):
    # sad: a SHA the repo never saw resolves to no author (no note -> None),
    # never raises.
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    assert resolve_author(tmp_path, "0" * 40) is None


def test_append_attested_unattested_sha_yields_none_author_but_still_chains(tmp_path):
    # edge: append_attested over an unattested commit writes a real row whose
    # author is None; the row still links the hash-chain and verifies.
    _init(tmp_path)
    sha = _commit(tmp_path, "a.py")             # NOT attested
    led = RunLedger(tmp_path / "run.jsonl")
    led.append(event="run-start", status="complete")
    e = led.append_attested(tmp_path, sha, event="confirmed-work", subject="u1",
                            status="pass")
    assert e.author is None
    rows = led.read()
    assert rows[-1].prev == rows[-2].entry_sha   # chained to the run-start row
    assert led.verify() is True


def test_append_attested_author_is_tamper_evident(tmp_path):
    # sad: the attested author is part of entry_sha (STEP-1 hashes `author`),
    # so rewriting it on disk breaks verify().
    _init(tmp_path)
    base = _commit(tmp_path, "a.py")
    sha = _commit(tmp_path, "b.py")
    attest.attest_commits(tmp_path, "codex", base, sha)
    p = tmp_path / "run.jsonl"
    led = RunLedger(p)
    led.append_attested(tmp_path, sha, event="confirmed-work", subject="u1",
                        status="pass", independence=True)
    assert RunLedger(p).verify() is True
    p.write_text(p.read_text().replace('"author": "codex"', '"author": "claude"'))
    assert RunLedger(p).verify() is False


def test_public_append_still_rejects_caller_author(tmp_path):
    # the STEP-1 guard is untouched: append_attested is the ONLY author path.
    led = RunLedger(tmp_path / "run.jsonl")
    with pytest.raises(ValueError):
        led.append(event="confirmed-work", status="pass", author="codex")
