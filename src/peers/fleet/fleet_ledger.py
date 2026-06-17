"""fleet_ledger.py -- the hash-chained, git-committed fleet-ledger (Stage-7 STEP-2).

A thin wrapper over the spine :class:`peers.spine.ledger.RunLedger`: it records
WHICH run is on WHICH slot, each run's status, the write-ahead start-intents, and
-- load-bearing for F3 cascade-invalidation -- one ATTESTED ``propagation-edge``
row per satisfied (producer, consumer) dependency. By reusing ``RunLedger`` it
inherits the backwards-walkable hash chain + :meth:`RunLedger.verify` tamper
evidence for FREE and adds NO new ledger schema: fleet events are free ``event``
strings over the existing §2.3 row.

Three security-load-bearing rules (fail CLOSED / fail SAFE):

- **Independence is COMPUTED, never asserted.** ``record_propagation_edge`` writes
  the edge via :meth:`RunLedger.append_attested`, whose ``author`` is the
  substrate-attested peer of ``tip_sha`` -- and sets ``independence = author is
  not None``, NEVER a literal ``True``. An unattested tip => ``author=None`` =>
  ``independence=False`` (no ``independence=True``/``author=None`` poison row that
  the consumer authorship gate would then trust; blocker F3-edge-2).
- **The edge parse fails CLOSED.** A torn ``propagation-edge`` row (missing
  ``from_run``/``to_run``) is SKIPPED and flagged with a ``malformed-edge`` marker
  -- never a silent ``KeyError`` that aborts the whole cascade (minor F3-edge-3).
- **A start-intent is VISIBLE.** ``record_start_intent`` writes BOTH an intent row
  AND a ``run-status`` row so ``latest_status``/``slot_of`` see the open intent and
  the scheduler counts its slot busy + projects its cost across ticks (blocker
  F5-2: otherwise the slot looks free => cross-tick overspend + double-start).
"""
from __future__ import annotations

from pathlib import Path

from peers.spine.ledger import LedgerEntry, RunLedger

#: run-status values that CLOSE an open start-intent (the run actually started or
#: reached a terminal state -- the write-ahead reconcile no longer re-attempts it).
_INTENT_CLOSING = frozenset({"running", "converged", "failed", "landed", "rejected"})


class FleetLedger:
    """A fleet-scoped hash-chained ledger over the spine :class:`RunLedger`."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._led = RunLedger(self.path)

    # ---- reading -------------------------------------------------------
    def read(self) -> list[LedgerEntry]:
        return self._led.read()

    def verify(self) -> bool:
        return self._led.verify()

    # ---- writing -------------------------------------------------------
    def record_status(self, run_id: str, status: str, *, slot: str | None = None) -> None:
        """Append a ``run-status`` row (append-only; ``latest_status`` is last-wins)."""
        self._led.append(
            event="run-status", status=status, subject=run_id,
            witness={"slot": slot} if slot else None, mode_run=run_id,
        )

    def record_slot(self, run_id: str, slot: str) -> None:
        """Pin ``run_id`` to ``slot`` (a slot witness ``slot_of`` reads)."""
        self._led.append(
            event="slot-assigned", status="ok", subject=run_id,
            witness={"slot": slot}, mode_run=run_id,
        )

    def record_propagation_edge(
        self, from_run: str, to_run: str, artifact: str, *, repo: Path | str, tip_sha: str,
    ) -> None:
        """Record an ATTESTED ``producer -> consumer`` edge (blocker F3-edge-2/-3).

        Idempotent / de-duplicated by ``(from_run, to_run)``: the conductor records
        an edge per (producer, consumer) pair every tick a consumer is scheduled --
        a pair already live yields no second row. ``repo`` MUST be the repo where
        ``tip_sha`` is attested (for a cross-tool edge that is the CONSUMER repo the
        producer tip was propagated into -- ``refs/notes/peers-attest`` is repo-local).
        Independence is COMPUTED from the substrate author, NEVER a literal ``True``.
        """
        if (from_run, to_run) in {(f, t) for f, t, _a in self.propagation_edges()}:
            return
        # Defense in depth (mirror propagate.py's independence rule EXACTLY): an
        # unattested tip => author None => independence False -- no poison row.
        from peers.spine.authorship import resolve_author

        author = resolve_author(repo, tip_sha)
        self._led.append_attested(
            repo, tip_sha, event="propagation-edge", status="ok",
            subject=f"{from_run}->{to_run}",
            witness={"from_run": from_run, "to_run": to_run,
                     "artifact": artifact, "tip": tip_sha},
            mode_run=to_run, independence=author is not None,
        )

    def record_intra_edge(self, from_run: str, to_run: str, artifact: str) -> None:
        """Record an INTRA-tool producer->consumer cascade edge whose producer has
        NO propagatable git-sha (a research file / find-bugs finding — its
        ``_converged_commit`` is ``None``).

        Unlike ``record_propagation_edge`` there is no git-sha to attest, so this
        writes a PLAIN (non-attested) ``propagation-edge`` row: the cascade walk
        (``propagation_edges`` / ``cascade_invalidate``) only needs ``from_run``/
        ``to_run``, and an intra-tool edge never crosses a peer boundary so
        ``independence`` is N/A (it is NOT asserted — never a poison ``True``).
        De-duplicated by ``(from_run, to_run)``, matching ``record_propagation_edge``
        (so a later real-sha edge for the same pair is the dedup no-op, never a torn
        double row). The witness keeps ``from_run``/``to_run`` non-empty so the
        ``_has_undeterminable_edge`` torn-row guard does NOT trip on it.
        """
        if (from_run, to_run) in {(f, t) for f, t, _a in self.propagation_edges()}:
            return
        self._led.append(
            event="propagation-edge", status="ok", subject=f"{from_run}->{to_run}",
            witness={"from_run": from_run, "to_run": to_run, "artifact": artifact,
                     "tip": None, "kind": "intra-converged"},
            mode_run=to_run,
        )

    def record_start_intent(self, run_id: str, slot: str) -> None:
        """Write-ahead a start-intent (blocker F5-2: VISIBLE to latest_status/slot_of).

        Writes BOTH the intent witness AND a ``run-status`` row with
        ``status="start-intent"`` so the scheduler's busy/running set and ``slot_of``
        account for the open intent across ticks (otherwise the intent slot looks
        FREE next tick => cross-tick overspend + double-start).
        """
        self._led.append(
            event="start-intent", status="pending", subject=run_id,
            witness={"slot": slot}, mode_run=run_id,
        )
        self._led.append(
            event="run-status", status="start-intent", subject=run_id,
            witness={"slot": slot}, mode_run=run_id,
        )

    def record_halt(self, reason: str) -> None:
        """Record a Tier-2 halt (a world-divergence / malformed-edge escalation)."""
        self._led.append(event="halt", status="halt", subject=reason)

    def supersede_edge(self, from_run: str, to_run: str) -> None:
        """Supersede a stale ``(from_run, to_run)`` edge (major F3-superseded).

        Called by the conductor on a successful repair-reconverge: the append-only
        log keeps the old ``propagation-edge`` row, but ``propagation_edges()``
        filters any ``(from, to)`` whose last supersede is later than its last record.
        """
        self._led.append(
            event="edge-superseded", status="ok", subject=f"{from_run}->{to_run}",
            witness={"from_run": from_run, "to_run": to_run}, mode_run=to_run,
        )

    def _record_malformed_edge(self, at: int, raw: dict | None) -> None:
        """Flag a torn ``propagation-edge`` row (minor F3-edge-3).

        ``at`` is the torn row's file position so the marker is recorded ONCE per
        row -- ``propagation_edges()`` converges (a later read sees the marker and
        skips re-appending). The conductor reads ``malformed-edge`` rows and
        escalates Tier-2 rather than silently emitting an empty cascade.
        """
        self._led.append(
            event="malformed-edge", status="halt",
            subject="unparseable-propagation-edge", witness={"at": at, "raw": raw},
        )

    # ---- readers (pure folds over read()) ------------------------------
    def latest_status(self, run_id: str) -> str | None:
        """The LAST ``run-status`` row for ``run_id`` (append-only, last-wins)."""
        status: str | None = None
        for r in self.read():
            if r.event == "run-status" and r.subject == run_id:
                status = r.status
        return status

    def slot_of(self, run_id: str) -> str | None:
        """The last slot witness for ``run_id`` (a slot-assigned/run-status/intent row)."""
        slot: str | None = None
        for r in self.read():
            if r.subject != run_id:
                continue
            if r.event in ("slot-assigned", "run-status", "start-intent"):
                w = r.witness or {}
                if isinstance(w, dict) and w.get("slot") is not None:
                    slot = w["slot"]
        return slot

    def propagation_edges(self) -> list[tuple[str, str, str | None]]:
        """The LIVE edges (fail-closed parse + superseded-aware; deduped by (from, to)).

        Walk in file order; an edge is LIVE iff its last ``propagation-edge`` record
        is later than its last ``edge-superseded``. A torn row (missing
        ``from_run``/``to_run``) is skipped and flagged ONCE -- never a KeyError.
        """
        rows = self.read()
        flagged = {r.witness.get("at") for r in rows
                   if r.event == "malformed-edge" and isinstance(r.witness, dict)}
        last_record: dict[tuple[str, str], int] = {}
        last_supersede: dict[tuple[str, str], int] = {}
        artifact: dict[tuple[str, str], str | None] = {}
        for i, r in enumerate(rows):
            # A TRUTHY non-dict witness (an agent-written/torn list/str/number row) is
            # normalized to {} -- NEVER `r.witness or {}`, which keeps the non-dict and
            # makes the `.get` below raise AttributeError, fail-OPENING the whole F3
            # cascade. Mirrors the isinstance guard in slot_of/intents/the malformed set.
            w = r.witness if isinstance(r.witness, dict) else {}
            if r.event == "propagation-edge":
                frm, to = w.get("from_run"), w.get("to_run")
                if not frm or not to:                # torn row OR a non-dict witness (w -> {})
                    if i not in flagged:             # idempotent: flag each torn row ONCE
                        self._record_malformed_edge(i, w)
                    continue                         # escalate, never silently drop
                last_record[(frm, to)] = i
                artifact[(frm, to)] = w.get("artifact")
            elif r.event == "edge-superseded":
                frm, to = w.get("from_run"), w.get("to_run")
                if frm and to:                       # a non-dict witness -> {} -> cannot supersede
                    last_supersede[(frm, to)] = i
        out: list[tuple[str, str, str | None]] = []
        for key, ri in last_record.items():
            if ri > last_supersede.get(key, -1):     # recorded AFTER any supersede -> live
                out.append((key[0], key[1], artifact[key]))
        return out

    def intents(self) -> list[tuple[str | None, str | None]]:
        """OPEN start-intents (the write-ahead reconcile input).

        An intent ``(run_id, slot)`` is OPEN iff no LATER ``run-status`` row for that
        ``run_id`` has a status in :data:`_INTENT_CLOSING`. The intent's OWN
        ``status="start-intent"`` run-status row does NOT close it -- only a
        terminal/active status does -- so it stays open until the real start lands a
        ``running`` row.
        """
        rows = self.read()
        out: list[tuple[str | None, str | None]] = []
        for i, r in enumerate(rows):
            if r.event != "start-intent":
                continue
            run_id = r.subject
            w = r.witness or {}
            slot = w.get("slot") if isinstance(w, dict) else None
            closed = any(
                later.event == "run-status" and later.subject == run_id
                and later.status in _INTENT_CLOSING
                for later in rows[i + 1:]
            )
            if not closed:
                out.append((run_id, slot))
        return out
