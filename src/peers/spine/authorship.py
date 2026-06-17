"""STEP-2 — substrate-attested authorship for ledger entries.

The ONLY trustworthy author signal is the substrate's HEAD-delta attestation
note (``refs/notes/peers-attest``), written by :mod:`peers.attest` from the
loop's *observed* commit range — not from anything the agent can type into a
commit trailer or a ledger field. This module is the thin seam the ledger uses
to turn a commit SHA into an author name.

Load-bearing invariant (tighten-only): an agent cannot author a ledger row for
itself. ``RunLedger.append`` rejects a caller-supplied ``author``; the only way
a non-None author reaches a row is :meth:`RunLedger.append_attested`, which
calls :func:`resolve_author` here. An unattested commit yields ``None`` — a
forged identity is ignored, never trusted.
"""
from __future__ import annotations

from pathlib import Path

from peers import attest


def resolve_author(repo: Path | str, sha: str) -> str | None:
    """Return the substrate-attested peer name for ``sha`` in ``repo``.

    Thin wrapper over :func:`peers.attest.attested_peer`: returns the attested
    peer name, or ``None`` when the commit carries no ``peers-attest`` note
    (unattested / forgeable identity) or the SHA is unknown to the repo. Never
    raises for a missing note or an unknown SHA — both resolve to ``None``.
    """
    return attest.attested_peer(Path(repo), sha)
