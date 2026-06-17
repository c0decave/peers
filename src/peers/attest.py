"""Substrate-side commit attribution.

The reviewer-checkoff anti-spoofing keys peer identity off the ``Peer: <name>``
trailer, which is agent-authored free text: a peer can stamp its own
implementation commit with the other peer's name and then self-checkoff. The
trailer is not a trustworthy identity signal.

This module records the *substrate's own* attribution instead: each commit is
attributed to the peer whose tick produced it, derived from the tick HEAD-delta
``(head_before, head_after]`` that the orchestrator observes in
``tick_loop._finalize_tick``. The attribution is written as a
``refs/notes/peers-attest`` note (value = peer name).

Why this is trustworthy without a secret: the peer is a one-shot CLI per tick
(``health.invoke`` runs it to process exit; BUG-134–138 tears down its process
group), so **no agent process is alive** when the orchestrator writes these
notes (post-tick) or when the goal engine later reads them (between ticks). A
note the agent forges *during* its tick is overwritten here from the observed
HEAD-delta. The agent controls a commit's content but not *when* it appears
relative to the tick boundary, so it cannot reattribute its own work.

Residual (documented, out of scope): tampering with historical notes while no
orchestrator is running requires host-level shell access — a different threat
model than the in-loop forger this defends against.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Short notes-ref name (the full ref is ``refs/notes/peers-attest``).
NOTES_REF = "peers-attest"

#: A reviewer signs off on an artifact by making a commit whose message
#: carries this marker line (``peers-review: <file>``). The substrate
#: attributes the commit to the reviewer via the peers-attest note; gates
#: SEARCH reachable history for it (FU-2) rather than trusting an
#: agent-authored free-text reviewer field.
_REVIEW_MARKER_RE = re.compile(r"^peers-review:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def _commits_between(repo: Path, since: str | None, until: str | None) -> list[str]:
    """Return the SHAs in ``(since, until]``, oldest-first."""
    if not since or not until:
        return []
    r = _git(repo, "rev-list", "--reverse", f"{since}..{until}")
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def commits_in_range(repo: Path, since: str | None) -> list[str]:
    """Return the SHAs in ``(since, HEAD]``, oldest-first.

    Unlike ``comm_layer.GitCommLayer.new_commits_by`` this does NOT filter on
    the ``Peer:`` trailer — every commit the tick produced is returned,
    regardless of (possibly forged) commit content. Returns ``[]`` when
    ``since`` is falsy (bootstrap: no prior HEAD to diff against) or on any git
    error.
    """
    return _commits_between(repo, since, "HEAD")


def attest_commits(
    repo: Path, peer: str, since_sha: str | None, head_sha: str | None,
) -> list[str]:
    """Attribute every commit in ``(since_sha, head_sha]`` to ``peer``.

    Writes ``refs/notes/peers-attest = peer`` on each commit, idempotently
    (``git notes add -f``) so a note the agent forged during its tick is healed
    from the substrate's observed HEAD-delta. Returns the list of attested SHAs.

    No-op (returns ``[]``) when ``since_sha`` is falsy (first tick / bootstrap)
    or the range is empty (the tick produced no commits).
    """
    shas = _commits_between(repo, since_sha, head_sha)
    for sha in shas:
        _git(repo, "notes", f"--ref={NOTES_REF}", "add", "-f", "-m", peer, sha)
    return shas


def attested_peer(repo: Path, sha: str) -> str | None:
    """Return the substrate-attested peer name for ``sha``, or ``None`` when the
    commit carries no ``peers-attest`` note."""
    r = _git(repo, "notes", f"--ref={NOTES_REF}", "show", sha)
    if r.returncode != 0:
        return None
    value = r.stdout.strip()
    return value or None


def is_ancestor(repo: Path, sha: str | None, of: str = "HEAD") -> bool:
    """True iff ``sha`` is an ancestor of (reachable from) ``of``.

    FU-2: ``attest_commits`` only heals notes in the observed tick range and
    never prunes notes minted on out-of-range / dangling commit-tree objects,
    so a note on a dangling commit survives ``git gc``. A peer that cites such
    a commit must not have it honored — this reachability gate (mirroring the
    checkoff gate's "walk reachable history" discipline) rejects it. Empty,
    unknown, or otherwise un-resolvable ``sha`` is treated as not-reachable
    (fail-closed).
    """
    if not sha:
        return False
    r = _git(repo, "merge-base", "--is-ancestor", sha, of)
    return r.returncode == 0


def reachable_attested_peer(repo: Path, sha: str | None) -> str | None:
    """``attested_peer(sha)`` but ONLY when ``sha`` is reachable from HEAD.

    Defeats the dangling/out-of-range note forge: a ``peers-attest`` note on a
    side commit-tree object is ignored even though the note exists, because the
    commit is not part of the run's reachable history.
    """
    if not is_ancestor(repo, sha, "HEAD"):
        return None
    return attested_peer(repo, sha)  # type: ignore[arg-type]


def attested_authors_of_file(repo: Path, path: str) -> set[str]:
    """Return the set of substrate-attested peers that modified ``path`` across
    reachable history (``git log HEAD -- path``).

    FU-2: used to exclude self-review. The single-most-recent-editor variant is
    UNSOUND — a peer can author a shortcut, have the co-peer make a trivial edit
    (becoming the last editor), then self-review (the author is no longer
    excluded). Excluding EVERY attested author of the file closes that
    laundering hole. For line-precise gates prefer
    :func:`attested_line_author_peers` (excludes only the marker's author, so a
    co-peer who edited OTHER lines can still review).
    """
    r = _git(repo, "log", "--format=%H", "HEAD", "--", path)
    if r.returncode != 0:
        return set()
    out: set[str] = set()
    for sha in r.stdout.split():
        peer = attested_peer(repo, sha)
        if peer:
            out.add(peer)
    return out


def attested_line_author_peers(
    repo: Path, path: str, linenos: list[int],
) -> set[str]:
    """Return the set of substrate-attested peers that introduced the given
    ``linenos`` of ``path`` (``git blame`` per line, at HEAD).

    FU-2 self-review exclusion, line-precise: the peer who wrote a shortcut /
    skip marker line must not review it, but a co-peer who only edited OTHER
    lines of the same file legitimately can. Lines git cannot attribute to a
    committed commit (uncommitted / out-of-range) contribute nothing; the
    caller falls back to the whole-file author set when this yields nothing.
    """
    out: set[str] = set()
    for ln in linenos:
        if ln < 1:
            continue
        r = _git(repo, "blame", "-L", f"{ln},{ln}", "--porcelain", "HEAD",
                 "--", path)
        if r.returncode != 0 or not r.stdout:
            continue
        sha = r.stdout.split(" ", 1)[0].strip()
        # An uncommitted line blames to all-zeros; a real commit is 40 hex.
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            continue
        if sha == "0" * 40:
            continue
        peer = attested_peer(repo, sha)
        if peer:
            out.add(peer)
    return out


def find_review_commit(
    repo: Path, artifact: str,
    exclude_peer: str | set[str] | frozenset[str] | None = None,
) -> str | None:
    """Return the peer who reviewed ``artifact`` via a substrate-attested
    ``peers-review: <artifact>`` commit, or ``None`` if no such review exists.

    FU-2 (P0 review 2026-06-12): the justifications.log reviewer field is
    agent-authored free text bound to no unforgeable identity, so a peer could
    self-bless a shortcut/skip/checkoff by naming the other peer. This mirrors
    the checkoff gate's ``_find_checkoff_commit``: the reviewer signs off by
    making a commit whose message carries ``peers-review: <artifact>``, the
    substrate attributes it (peers-attest note), and the gate SEARCHES
    reachable history for it — the agent never chooses the sha. That closes
    all three forge variants:

    * (A) a dangling/unreachable forged note — only ``git log HEAD`` (reachable
      history) is searched, so a side/dangling review commit is never found;
    * (B) a free-form reviewer with no backing — the commit must carry a real
      ``peers-attest`` note written by the substrate;
    * (C) citing an unrelated attested commit — the commit must carry the
      ``peers-review: <artifact>`` marker for THIS artifact (exact match).

    ``exclude_peer`` filters out self-review: a single peer name OR a SET of
    peer names (the artifact's author set — see ``attested_authors_of_file`` /
    ``attested_line_author_peers``) that may NOT count as the reviewer. A set is
    required to close the multi-author laundering hole (excluding only the last
    editor lets the real author self-review). Returns the first matching
    reviewer peer in ``git log`` order (newest first).
    """
    if exclude_peer is None:
        excluded: set[str] = set()
    elif isinstance(exclude_peer, str):
        excluded = {exclude_peer}
    else:
        excluded = set(exclude_peer)
    r = _git(repo, "log", "--format=%H%x1f%B%x1e", "HEAD")
    if r.returncode != 0:
        return None
    for record in r.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _sep, body = record.partition("\x1f")
        sha = sha.strip()
        if not sha:
            continue
        if not any(m.strip() == artifact
                   for m in _REVIEW_MARKER_RE.findall(body)):
            continue
        # sha came from `git log HEAD` so it is reachable by construction; the
        # peers-attest note supplies the unforgeable reviewer identity.
        peer = attested_peer(repo, sha)
        if peer and peer not in excluded:
            return peer
    return None
