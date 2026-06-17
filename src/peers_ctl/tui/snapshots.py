"""Immutable view models the TUI windows render. Pure data — no I/O, no Textual."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PEER_STATES = ("healthy", "degraded", "halted", "unavailable")


@dataclass(frozen=True)
class GateView:
    id: str
    kind: str            # "hard" | "soft"
    state: str           # hard: "pass"|"fail"|"unknown"; soft: "pending"|"reached"
    stuck: int           # consecutive-fail count (hard); 0 if not stuck
    duration_ms: int
    diagnostic: str
    cached: bool
    consensus: tuple[int, int] | None  # soft only: (count, needed)


@dataclass(frozen=True)
class PeerView:
    name: str
    state: str
    consecutive_fails: float
    recent_runs: list[Any]   # entries are bool | float (0.5 = productive-no-handoff)
    last_run: dict[str, Any]  # SPARSE — always read with .get()


@dataclass(frozen=True)
class BudgetView:
    spent_runtime_s: int
    max_runtime_s: int | None
    spent_tokens: int
    max_tokens: int | None
    spent_usd: float
    max_usd: float | None
    max_usd_mode: str | None
    max_usd_mode_reason: str | None
    consecutive_failures: int
    wasted_runtime: list[dict[str, Any]]  # capped(20) list of {iteration,peer,duration_s}


@dataclass(frozen=True)
class TickEntry:
    iteration: int | None
    peer: str | None
    classification: str | None
    success: bool | None
    tokens: int
    usd: float
    head_before: str | None
    head_after: str | None
    warnings: list[str]
    ts: str
    is_exit: bool = False
    exit_reason: str | None = None


@dataclass(frozen=True)
class GateSnapshotRow:
    """One past tick's reconstructed gate stand, parsed from the per-tick
    ``gates`` field of ``runs.jsonl`` (Wave-2 §5.2).

    ``gates`` is the compact substrate map ``{"hard": {id: state}, "soft":
    {id: "n/m"}}`` (possibly with a ``"_truncated"`` marker). ``green``/``total``
    are pre-counted for the header. ``gap_s`` is the seconds since the PREVIOUS
    snapshot row (the tick duration/gap), ``None`` for the first row or an
    unparseable ts. Only ticks that CARRY a ``gates`` field become rows; the
    synthetic ``exit`` line and pre-Wave-2 lines without ``gates`` are skipped."""
    iteration: int | None
    ts: str
    gates: dict[str, Any]   # {"hard": {...}, "soft": {...}, "_truncated"?: bool}
    green: int
    total: int
    gap_s: float | None     # seconds since the previous snapshot row; None if first/unknown


@dataclass(frozen=True)
class ConvergenceView:
    consecutive_clean_ticks: int
    convergence_phase: str | None      # implement-mode only; None otherwise
    phase_b_extra_ticks: int | None    # implement-mode only


@dataclass(frozen=True)
class RunSnapshot:
    name: str
    state_present: bool
    iteration: int
    mode: str | None
    phase: str | None
    current_peer: str | None
    gates: list[GateView] = field(default_factory=list)
    peers: list[PeerView] = field(default_factory=list)
    budget: BudgetView | None = None
    convergence: ConvergenceView | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FleetEntry:
    name: str
    path: str
    state: str           # fresh|running|stopped|crashed|unknown (from registry, no reconcile)
    pid: int | None
    iteration: int | None
    gates_green: int | None
    gates_total: int | None
    alert: bool          # HALTED.md present or pending warnings


@dataclass(frozen=True)
class CommitReviewRow:
    sha: str
    subject: str
    trailers: dict[str, str]           # parsed git-style trailers (may be empty)
    trailer_peer: str | None           # trailers.get("Peer") — agent-authored, FORGEABLE
    attested_peer: str | None          # peers-attest note — substrate-attested, trustworthy
    # True only when an attestation EXISTS and matches the trailer. Absence of
    # an attestation is attest_match=False (NOT a forgery alarm); a present
    # attestation that != trailer_peer is the forgery signal (attest_match=False).
    attest_match: bool


@dataclass(frozen=True)
class BugView:
    id: str
    severity: str
    title: str
    status: str
    filed_tick: int | None
    resolved_tick: int | None
    author: str | None


@dataclass(frozen=True)
class PlanStep:
    """One PLAN.md checklist item: done-bool + the step text.

    ``author`` is the cheaply-available checkoff author *if* the line carries one
    (the canonical format ``- [x] [STEP-N] text (sha)`` does not, so it is usually
    ``None`` — the trustworthy author is the substrate attestation, surfaced in
    the Konsens/review window, not here)."""
    done: bool
    text: str
    author: str | None = None


@dataclass(frozen=True)
class LogRow:
    """One merged log row for the Log window.

    ``kind`` is ``"warning"`` (from ``state['warnings_history']``) or ``"stop"``
    (from ``.peers/last-stop-reason.txt``). ``iteration`` is the tick the row
    belongs to when known (warnings carry it; the stop sentinel does not)."""
    kind: str            # "warning" | "stop"
    text: str
    ts: str
    iteration: int | None = None


@dataclass(frozen=True)
class SpineRunEntry:
    mode_run: str | None
    worktree_path: str | None
    branch: str | None
    ledger_path: str | None
    pid: int | None
    started_at: str | None


@dataclass(frozen=True)
class AutonomyLedgerView:
    """Re-derived autonomy/spine ledger summary. ``independence`` is NEVER read
    from a stored flag — gates/convergence are re-derived from the rows."""
    verified: bool | None              # ledger.verify() integrity badge; None when absent
    gates: dict[str, bool]             # re-derived evaluate_spine_gates() result
    converged: bool                    # re-derived is_converged()
    dry_streak: int                    # re-derived dry_streak()
    events: list[dict[str, Any]]       # summarized rows (event/status/author/independence)
