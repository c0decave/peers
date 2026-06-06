"""Tests for substrate-side commit attribution.

The substrate attributes each commit to the peer whose tick produced it,
derived from the tick HEAD-delta, and records it as a ``refs/notes/peers-attest``
note. This is the agent-unforgeable identity signal the reviewer-checkoff gate
keys off of, overriding the agent-authored ``Peer:`` trailer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers import attest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _init(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "dash@localhost.local")
    _git(repo, "config", "user.name", "dash")


def _commit(repo: Path, name: str, body: str = "c") -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", body)
    return _git(repo, "rev-parse", "HEAD").strip()


def test_attest_and_read_back(tmp_path):
    _init(tmp_path)
    a = _commit(tmp_path, "a.py")
    b = _commit(tmp_path, "b.py")
    attest.attest_commits(tmp_path, "claude", a, b)
    assert attest.attested_peer(tmp_path, b) == "claude"


def test_attest_covers_every_commit_in_range(tmp_path):
    _init(tmp_path)
    base = _commit(tmp_path, "base.py")
    c1 = _commit(tmp_path, "c1.py")
    c2 = _commit(tmp_path, "c2.py")
    attest.attest_commits(tmp_path, "codex", base, c2)
    assert attest.attested_peer(tmp_path, c1) == "codex"
    assert attest.attested_peer(tmp_path, c2) == "codex"
    # The base commit is the `since` boundary and must NOT be attributed.
    assert attest.attested_peer(tmp_path, base) is None


def test_attest_commits_stops_at_observed_head(tmp_path):
    """BUG-144: attest_commits receives the orchestrator's observed
    head_sha, so it must not attribute commits that appear after that
    snapshot. Otherwise a concurrent/manual commit between the HEAD
    capture and notes write could be misattributed to the peer whose
    tick just ended."""
    _init(tmp_path)
    base = _commit(tmp_path, "base.py")
    observed = _commit(tmp_path, "observed.py")
    later = _commit(tmp_path, "later.py")

    written = attest.attest_commits(tmp_path, "codex", base, observed)

    assert written == [observed]
    assert attest.attested_peer(tmp_path, observed) == "codex"
    assert attest.attested_peer(tmp_path, later) is None


def test_attest_none_since_is_noop(tmp_path):
    _init(tmp_path)
    a = _commit(tmp_path, "a.py")
    attest.attest_commits(tmp_path, "claude", None, a)
    assert attest.attested_peer(tmp_path, a) is None


def test_attest_heals_forged_note(tmp_path):
    """An in-tick forged note is overwritten by the substrate's post-tick
    attribution (idempotent ``git notes add -f``)."""
    _init(tmp_path)
    base = _commit(tmp_path, "base.py")
    impl = _commit(tmp_path, "impl.py")
    # Agent forges a note during its tick attributing its work to the other peer.
    _git(tmp_path, "notes", f"--ref={attest.NOTES_REF}", "add", "-f", "-m",
         "codex", impl)
    # Substrate heals it from the observed HEAD-delta (claude's tick).
    attest.attest_commits(tmp_path, "claude", base, impl)
    assert attest.attested_peer(tmp_path, impl) == "claude"


def test_attested_peer_unattested_is_none(tmp_path):
    _init(tmp_path)
    a = _commit(tmp_path, "a.py")
    assert attest.attested_peer(tmp_path, a) is None


def test_commits_in_range_oldest_first_excludes_since(tmp_path):
    _init(tmp_path)
    base = _commit(tmp_path, "base.py")
    c1 = _commit(tmp_path, "c1.py")
    c2 = _commit(tmp_path, "c2.py")
    assert attest.commits_in_range(tmp_path, base) == [c1, c2]


def test_commits_in_range_none_since_is_empty(tmp_path):
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    assert attest.commits_in_range(tmp_path, None) == []


def test_commits_in_range_invalid_since_returns_empty(tmp_path):
    """sad: a non-existent ``since`` SHA must NOT crash and must NOT
    silently fall through to "all commits" — git rev-list will fail
    with rc!=0 and ``commits_in_range`` returns ``[]``. The
    orchestrator relies on this fail-closed contract: bogus delta →
    nothing attributed, rather than attributing the whole history to
    one peer."""
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    assert attest.commits_in_range(tmp_path, "deadbeef" * 5) == []


def test_attested_peer_unknown_sha_returns_none(tmp_path):
    """sad: ``attested_peer`` for a SHA git doesn't recognise must
    return ``None`` (not raise) — the reviewer-checkoff gate uses the
    None result to mean "no substrate attribution, fall back to
    trailer". Crashing would convert a single bad lookup into a hard
    orchestrator halt."""
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    assert attest.attested_peer(tmp_path, "0" * 40) is None


def test_attest_commits_on_invalid_since_is_noop(tmp_path):
    """sad: ``attest_commits`` with a bogus ``since_sha`` must NOT
    attribute anything — ``commits_in_range`` returns ``[]`` and the
    write loop is skipped. Defends against the orchestrator attributing
    a peer to commits it didn't make when HEAD-tracking glitches."""
    _init(tmp_path)
    a = _commit(tmp_path, "a.py")
    written = attest.attest_commits(tmp_path, "claude", "deadbeef" * 5, a)
    assert written == []
    assert attest.attested_peer(tmp_path, a) is None
