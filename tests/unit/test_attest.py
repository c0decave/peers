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


# --- FU-2: reachability + attested review-commit search --------------------
#
# The reviewer-signoff escape (justifications.log) bound the reviewer to a
# free-form, agent-authored text field with no unforgeable identity (P0 review
# 2026-06-12, HIGH). The sound fix mirrors checkoff's _find_checkoff_commit:
# the reviewer signs off by making a `peers-review: <file>` commit that the
# substrate attributes (peers-attest note), and the gate SEARCHES REACHABLE
# history for it — the agent never chooses the sha. This closes:
#   (A) dangling/unreachable forged note   — only `git log HEAD` is searched
#   (B) free-form reviewer, no backing      — must be substrate-attested
#   (C) cite an unrelated attested commit   — must carry `peers-review: <file>`


def _attest_one(repo: Path, sha: str, peer: str) -> None:
    parent = _git(repo, "rev-parse", f"{sha}^").strip()
    attest.attest_commits(repo, peer, parent, sha)


def _review_commit(repo: Path, name: str, file_token: str, peer: str) -> str:
    sha = _commit(repo, name, f"peers-review: {file_token}\n\nLGTM")
    _attest_one(repo, sha, peer)
    return sha


def test_is_ancestor_true_for_reachable(tmp_path):
    _init(tmp_path)
    a = _commit(tmp_path, "a.py")
    b = _commit(tmp_path, "b.py")
    assert attest.is_ancestor(tmp_path, a, "HEAD") is True
    assert attest.is_ancestor(tmp_path, b, "HEAD") is True


def test_is_ancestor_false_for_unreachable_and_bogus(tmp_path):
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    # a dangling commit-tree object, never on any ref → not an ancestor
    tree = _git(tmp_path, "write-tree").strip()
    dangling = _git(tmp_path, "commit-tree", tree, "-m", "side").strip()
    assert attest.is_ancestor(tmp_path, dangling, "HEAD") is False
    assert attest.is_ancestor(tmp_path, "", "HEAD") is False
    assert attest.is_ancestor(tmp_path, "0" * 40, "HEAD") is False


def test_reachable_attested_peer_ignores_unreachable_note(tmp_path):
    # sad (A): a peers-attest note minted on a DANGLING commit must NOT be
    # honored — reachable_attested_peer returns None even though the note
    # exists, defeating the out-of-range/dangling-note forge.
    _init(tmp_path)
    _commit(tmp_path, "a.py")
    tree = _git(tmp_path, "write-tree").strip()
    dangling = _git(tmp_path, "commit-tree", tree, "-m", "side").strip()
    _git(tmp_path, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex",
         dangling)
    assert attest.attested_peer(tmp_path, dangling) == "codex"  # note exists
    assert attest.reachable_attested_peer(tmp_path, dangling) is None  # but unreachable


def test_find_review_commit_happy(tmp_path):
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    _review_commit(tmp_path, "r1.py", "src/foo.py", "codex")
    assert attest.find_review_commit(
        tmp_path, "src/foo.py", exclude_peer="claude") == "codex"


def test_find_review_commit_none_when_no_review(tmp_path):
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    assert attest.find_review_commit(tmp_path, "src/foo.py") is None


def test_find_review_commit_rejects_unattested(tmp_path):
    # sad (B): a peers-review commit with NO substrate attestation is ignored —
    # the agent cannot self-bless by writing the marker without the note.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    _commit(tmp_path, "r1.py", "peers-review: src/foo.py\n\nLGTM")  # not attested
    assert attest.find_review_commit(tmp_path, "src/foo.py") is None


def test_find_review_commit_rejects_unrelated_attested_commit(tmp_path):
    # sad (C): a genuinely-attested commit by codex that does NOT carry the
    # `peers-review: src/foo.py` token must not count as a review of foo.py —
    # a peer cannot cite an unrelated handoff/work commit as the sign-off.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    sha = _commit(tmp_path, "work.py", "normal work by codex")
    _attest_one(tmp_path, sha, "codex")
    assert attest.find_review_commit(tmp_path, "src/foo.py") is None


def test_find_review_commit_rejects_unreachable_review(tmp_path):
    # sad (A): a `peers-review` commit on a side line not reachable from HEAD
    # is not found (only reachable history is searched).
    _init(tmp_path)
    base = _commit(tmp_path, "base.py")
    # build a dangling review commit-tree attested to codex
    tree = _git(tmp_path, "write-tree").strip()
    dangling = _git(
        tmp_path, "commit-tree", tree, "-p", base, "-m",
        "peers-review: src/foo.py").strip()
    _git(tmp_path, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex",
         dangling)
    assert attest.find_review_commit(tmp_path, "src/foo.py") is None


def test_find_review_commit_excludes_self_reviewer(tmp_path):
    # sad: a peer cannot review its own file — exclude_peer filters it out.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    _review_commit(tmp_path, "r1.py", "src/foo.py", "claude")
    assert attest.find_review_commit(
        tmp_path, "src/foo.py", exclude_peer="claude") is None
    # but a different file's reviewer is unaffected
    assert attest.find_review_commit(
        tmp_path, "src/foo.py", exclude_peer="codex") == "claude"


def test_reachable_attested_peer_returns_peer_for_reachable(tmp_path):
    # happy path (review finding): reachable_attested_peer returns the peer for
    # a reachable, attested commit (the None branch is covered above).
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    c = _commit(tmp_path, "c.py")
    _attest_one(tmp_path, c, "codex")
    assert attest.reachable_attested_peer(tmp_path, c) == "codex"


def test_attested_authors_of_file_collects_every_editor(tmp_path):
    # the file's author set = EVERY attested peer that touched it (not just the
    # most recent editor) — closes the multi-author self-review laundering hole.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    (tmp_path / "foo.py").write_text("a\nb\n")
    _git(tmp_path, "add", "foo.py")
    _git(tmp_path, "commit", "-q", "-m", "claude foo")
    s1 = _git(tmp_path, "rev-parse", "HEAD").strip()
    _attest_one(tmp_path, s1, "claude")
    (tmp_path / "foo.py").write_text("a\nb\nc\n")  # codex appends (trivial)
    _git(tmp_path, "add", "foo.py")
    _git(tmp_path, "commit", "-q", "-m", "codex foo")
    s2 = _git(tmp_path, "rev-parse", "HEAD").strip()
    _attest_one(tmp_path, s2, "codex")
    assert attest.attested_authors_of_file(tmp_path, "foo.py") == {"claude", "codex"}
    assert attest.attested_authors_of_file(tmp_path, "missing.py") == set()


def test_attested_line_author_peers_blames_specific_lines(tmp_path):
    # line-precise authorship: line 1 (the marker) stays claude's even after
    # codex appends line 2 — so a marker author can be excluded WITHOUT
    # over-excluding a co-peer who only edited other lines.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    (tmp_path / "foo.py").write_text("marker-line\n")
    _git(tmp_path, "add", "foo.py")
    _git(tmp_path, "commit", "-q", "-m", "claude marker")
    s1 = _git(tmp_path, "rev-parse", "HEAD").strip()
    _attest_one(tmp_path, s1, "claude")
    (tmp_path / "foo.py").write_text("marker-line\nappended\n")  # codex, line 2
    _git(tmp_path, "add", "foo.py")
    _git(tmp_path, "commit", "-q", "-m", "codex append")
    s2 = _git(tmp_path, "rev-parse", "HEAD").strip()
    _attest_one(tmp_path, s2, "codex")
    assert attest.attested_line_author_peers(tmp_path, "foo.py", [1]) == {"claude"}
    assert attest.attested_line_author_peers(tmp_path, "foo.py", [2]) == {"codex"}
    assert attest.attested_line_author_peers(tmp_path, "missing.py", [1]) == set()


def test_find_review_commit_excludes_a_set_of_peers(tmp_path):
    # find_review_commit accepts a SET of excluded peers (the file/line author
    # set), not just one — so the marker author is excluded even when a co-peer
    # is the file's last editor.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    _review_commit(tmp_path, "r1.py", "src/foo.py", "claude")
    assert attest.find_review_commit(
        tmp_path, "src/foo.py", exclude_peer={"claude", "codex"}) is None
    assert attest.find_review_commit(
        tmp_path, "src/foo.py", exclude_peer={"codex"}) == "claude"


def test_find_review_commit_exact_token_match_edge(tmp_path):
    # edge: the token must match the artifact EXACTLY — a review of
    # `src/foobar.py` does not satisfy a query for `src/foo.py` (no prefix
    # confusion), and surrounding whitespace in the marker is tolerated.
    _init(tmp_path)
    _commit(tmp_path, "base.py")
    sha = _commit(tmp_path, "r1.py", "peers-review:   src/foobar.py  \n\nok")
    _attest_one(tmp_path, sha, "codex")
    assert attest.find_review_commit(tmp_path, "src/foo.py") is None
    assert attest.find_review_commit(
        tmp_path, "src/foobar.py", exclude_peer="claude") == "codex"
