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

import subprocess
from pathlib import Path

#: Short notes-ref name (the full ref is ``refs/notes/peers-attest``).
NOTES_REF = "peers-attest"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def commits_in_range(repo: Path, since: str | None) -> list[str]:
    """Return the SHAs in ``(since, HEAD]``, oldest-first.

    Unlike ``comm_layer.GitCommLayer.new_commits_by`` this does NOT filter on
    the ``Peer:`` trailer — every commit the tick produced is returned,
    regardless of (possibly forged) commit content. Returns ``[]`` when
    ``since`` is falsy (bootstrap: no prior HEAD to diff against) or on any git
    error.
    """
    if not since:
        return []
    r = _git(repo, "rev-list", "--reverse", f"{since}..HEAD")
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


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
    shas = commits_in_range(repo, since_sha)
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
