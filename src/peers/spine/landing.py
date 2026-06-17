"""Stage-4 — the explicit LANDING CONTRACT.

Today develop emits one thin ``landing`` row (witness kind ``url``, not
gate-checked). Stage 4 makes landing a VERIFIABLE record: a structured
:class:`LandingContract` saying which branch/artifact WOULD be mergeable and
WHICH spine gates made it so — derived FROM the run's own ledger gate decisions
(``gates.evaluate_spine_gates`` + ``all_pass``) plus a ``resolves_to_commit``-valid
head, NEVER from agent-authored text. It RECORDS mergeability; it does NOT merge.

Stage 6 (§6.3) replaces the Stage-4 unconditional clamp with the precise S2
decision: :func:`build_landing_contract` derives ``landing_mode == "auto-merge"``
iff the operator REQUESTED it AND the run is ``mergeable`` AND it is NOT
``self_hosting`` — otherwise the fail-closed ``branch-pr`` default (the Stage-4
universal clamp narrowed to the trusted case). The ``landing_mode`` and
``self_hosting`` params are now LOAD-BEARING (not the redundant Stage-4 seam): a
self-hosting run — its target IS peers, or its diff touches the spine/gates/
governance — is forced back to ``branch-pr`` + out-of-band human review.

The contract serialises via :meth:`LandingContract.to_witness` onto the existing
``url``-kind ``landing`` row as an ADVISORY record (the spine re-derives only
``file``/``git-sha`` witnesses, so appending it self-greens NO gate); the read
path (``DevelopFrontend.interpret``) RE-DERIVES the verdict from the live ledger,
never trusting the stored witness text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from peers.spine.gates import all_pass, evaluate_spine_gates, resolves_to_commit
from peers.spine.ledger import LedgerEntry

#: Verdict sentinels.
MERGEABLE = "mergeable"
NOT_MERGEABLE = "not-mergeable"


@dataclass
class LandingContract:
    """The structured landing record. ``mergeable`` and ``gates`` are SOURCED
    from the ledger gate decisions (never agent text); ``head_sha`` is the
    mergeable head (``resolves_to_commit``-valid); ``landing_mode`` is the Stage-6
    S2 verdict (``auto-merge`` iff requested + mergeable + not self_hosting, else
    the fail-closed ``branch-pr`` default). ``gates`` uses
    ``field(default_factory=dict)`` (a bare ``={}`` mutable default raises at
    class-definition time)."""

    branch: str | None
    mergeable: bool
    landing_mode: str
    mode_run: str
    gates: dict[str, bool] = field(default_factory=dict)
    head_sha: str | None = None
    self_hosting: bool = False

    def to_witness(self) -> dict:
        """Serialise to a ledger-safe ``url``-kind witness the existing ``landing``
        row can carry. The witness kind stays ``url`` (advisory — the spine
        re-derives only ``file``/``git-sha``, so this row is a RECORD, not a
        confirmed-work witness). The structured verdict rides INSIDE the
        ``contract`` sub-dict; all values are JSON-native so the ledger digest is
        stable. ``contract`` carries ``landing_mode`` (in addition to the sibling
        root ``landing``) so the develop e2e can assert the S2 verdict directly by
        reading ``contract['landing_mode']`` (always present, whatever the
        verdict)."""
        return {
            "kind": "url",
            "uri": self.branch,
            "landing": self.landing_mode,
            "contract": {
                "mergeable": self.mergeable,
                "gates": dict(self.gates),
                "head_sha": self.head_sha,
                "self_hosting": self.self_hosting,
                "landing_mode": self.landing_mode,
                "mode_run": self.mode_run,
            },
        }


def build_landing_contract(
    rows: "list[LedgerEntry]",
    *,
    repo: Path | str,
    mode_run: str,
    branch: str | None,
    head_sha: str | None,
    landing_mode: str = "branch-pr",
    self_hosting: bool = False,
    dry_n: int = 3,
) -> LandingContract:
    """Derive the landing contract FROM the ledger gate decisions.

    ``gates`` is ``evaluate_spine_gates(rows, ...)`` — the SOLE source of the
    per-gate map (kernel-owned facts, never agent text). ``mergeable`` is
    ``all_pass(gates)`` AND ``resolves_to_commit(repo, head_sha)`` (a fabricated /
    None head is never mergeable even if every gate passes). Stage 6:
    ``landing_mode == "auto-merge"`` iff requested + mergeable + not self_hosting,
    else the fail-closed ``branch-pr`` default. Never raises: a missing/None
    ``head_sha`` records ``mergeable=False`` (so the ``and mergeable`` term short-
    circuits to branch-pr), it does not crash."""
    # HONEST-01: anchor authorship-attest reachability on the run's branch (its
    # tip), NOT the repo HEAD — the converged commit lives on the isolated branch.
    gates = evaluate_spine_gates(rows, mode_run=mode_run, dry_n=dry_n, repo=repo,
                                 head=branch or "HEAD")
    head_ok = resolves_to_commit(Path(repo), head_sha)
    mergeable = all_pass(gates) and head_ok
    # Stage 6 (§6.3): auto-merge is enabled ONLY when the operator requested it AND
    # the run is mergeable AND it is NOT self-hosting -- otherwise the fail-closed
    # branch-pr default (the Stage-4 universal clamp narrowed to the trusted case).
    # Default-deny: only the exact "auto-merge" token enables it; every other input
    # (a failing gate, a fabricated/None head, an un-requested mode, OR a self-hosting
    # flag) lands branch-pr. Monotonic-deny: no input turns a non-mergeable or
    # self-hosting run into auto-merge.
    if landing_mode == "auto-merge" and mergeable and not self_hosting:
        mode = "auto-merge"
    else:
        mode = "branch-pr"
    return LandingContract(
        branch=branch, mergeable=mergeable, gates=gates, landing_mode=mode,
        mode_run=mode_run, head_sha=head_sha, self_hosting=self_hosting)
