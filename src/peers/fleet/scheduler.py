"""STEP-4 -- the pure pool/ceiling scheduler.

``startable_runs`` answers one question deterministically: given the program
(the DAG), the fleet-ledger (what is running / done), the compute ``Pool`` and
the aggregate ``Ceiling``, which runs may start RIGHT NOW and on which slot?

It is the compute-pool model, not fixed lanes ([07 §7.3]): a run is startable
when its deps are satisfied (via the INJECTED ``dep_check`` -- STEP-3's
``dep_satisfied`` in production, a stub in the unit tests; the scheduler draws
NO trust of its own), a slot is free (respecting affinity), and starting it
would not breach the fleet cost ceiling. It is PURE -- no I/O, no mutation of
the ledger; the caller (the STEP-6 conductor) acts on the returned
``ScheduleResult``.

invariants enforced here (one miss is a silent fleet-scale overspend):

* The ceiling is over the WHOLE fleet (per-run ``op_config.budget`` compose to
  nothing). A run whose projected aggregate cost would cross ``max_tokens``
  WAITS -- it is not startable.
* An UNBUDGETED run under a non-None ceiling is UNKNOWN cost, NOT free. It
  fails closed (it WAITS) -- never projected as 0, which would silently
  overspend an unmetered run (blocker F5-1).
* An OPEN ``start-intent`` (committed write-ahead by the conductor before the
  real ``SlotRunner.start``) counts as RUNNING: its slot is busy and its cost
  is projected across ticks, so a crash between intent and start is reconciled,
  never double-started / double-spent (blocker F5-2).
* ``assignments`` carries the exact ``(run_id, slot)`` PAIRS the scheduler
  costed; the conductor honors these and does NOT re-derive its own free slots
  and start everything (blocker F5-3).
* When the only remaining work cannot progress because of the ceiling --
  INCLUDING the case where it is ALSO slot-blocked but a ceiling is set --
  ``all_blocked_by_ceiling`` is True so the conductor surfaces a Tier-1 batched
  decision (raise the ceiling or stop), never an autonomous overrun or a silent
  stall (major F5-allblocked). The flag is gated on ``bool(ready)``: nothing
  ready for OTHER reasons (unsatisfied deps) is normal back-pressure, not a
  ceiling decision.

A ``Ceiling`` with ``max_tokens=None`` is an explicit opt-out of the aggregate
cap (per-run budgets still bound each run) -- not a silent infinity.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pool:
    """The compute pool: the slot ids and the affinity label->slot pinning."""

    slots: list[str] = field(default_factory=list)
    affinity: dict = field(default_factory=dict)       # label -> slot id


@dataclass
class Ceiling:
    """The aggregate fleet cap. ``None`` on an axis = no cap on that axis."""

    max_tokens: int | None = None
    max_runs: int | None = None


@dataclass
class ScheduleResult:
    """What the scheduler costed this tick.

    ``assignments`` is the ``[(run_id, slot)]`` the conductor MUST honor exactly
    (blocker F5-3). ``all_blocked_by_ceiling`` is the Tier-1 escalation signal.
    """

    startable: list[str] = field(default_factory=list)
    assignments: list = field(default_factory=list)
    all_blocked_by_ceiling: bool = False


# Sentinel: "this run's cost cannot be projected" (unbudgeted, no injected
# projection). Distinct from 0 so an unmetered run under a ceiling fails closed
# instead of being silently treated as free (blocker F5-1).
_UNKNOWN_COST = object()

# A cost that no finite ceiling can ever accommodate. An in-flight run whose
# cost is unknown under a finite ceiling already breached the fail-closed
# contract upstream; it contributes this so nothing new fits behind it.
_OVER_ANY_CEILING = 1 << 62


def _project(run_id, projected, by_id):
    """The projected token cost of ``run_id``.

    The injected ``projected`` dict wins (the test seam + the future live token
    meter from the substrate plugs in HERE). Else the run's real
    ``op_config.budget.max_tokens``. If THAT is unset (``None``), the cost is
    UNKNOWN -- return the sentinel, NEVER 0 (blocker F5-1).
    """
    if projected is not None and run_id in projected:
        return projected[run_id]
    spec = by_id.get(run_id)
    if spec is not None and spec.op_config.budget.max_tokens is not None:
        return spec.op_config.budget.max_tokens
    return _UNKNOWN_COST


def _project_or_inf(run_id, projected, by_id, ceilinged) -> int:
    """Project ``run_id`` to an int for SUMMING the in-flight cost.

    Unknown cost is irrelevant when there is no cap (return 0); under a finite
    ceiling an unknown in-flight cost already breached fail-closed, so it
    contributes a value no new run can fit behind.
    """
    cost = _project(run_id, projected, by_id)
    if cost is _UNKNOWN_COST:
        return _OVER_ANY_CEILING if ceilinged else 0
    return cost


def _occupied_slots(fleet_ledger) -> set:
    """Every slot the fleet-ledger shows held by a running / open-intent run.

    INCLUDING runs OUTSIDE the current program: the fleet-ledger is fleet-wide,
    so another program's running run (or an open start-intent) holds its slot and
    this program's scheduler must not assign it. Derived from the public readers
    (``read``/``latest_status``/``slot_of``) so the scheduler does not duplicate
    the ledger's row schema; a run is only counted while RUNNING or in an OPEN
    start-intent (a converged/failed/rejected run frees its slot). Not a hot path
    (the fleet is small); correctness over micro-optimisation.
    """
    occupied: set = set()
    run_ids = {r.subject for r in fleet_ledger.read()
               if r.event == "run-status" and r.subject}
    for rid in run_ids:
        if fleet_ledger.latest_status(rid) in ("running", "start-intent"):
            slot = fleet_ledger.slot_of(rid)
            if slot:
                occupied.add(slot)
    return occupied


def _pick_slot(spec, free_slots, pool, used):
    """The free slot ``spec`` may take, or ``None``.

    With an affinity label, ONLY the pinned slot satisfies it -- returned iff it
    is free (in ``free_slots``) and not already claimed this tick (``used``).
    With no affinity, the first free slot not yet claimed this tick.
    """
    if spec.affinity:
        pinned = pool.affinity.get(spec.affinity)
        if pinned is not None and pinned in free_slots and pinned not in used:
            return pinned
        return None
    for slot in free_slots:
        if slot not in used:
            return slot
    return None


def startable_runs(program, fleet_ledger, pool, ceiling, *, dep_check,
                   projected=None) -> ScheduleResult:
    """Return the in-ceiling, dep-satisfied, slot-fitting runs to start now.

    PURE: reads ``program``/``fleet_ledger``/``pool``/``ceiling`` and the
    injected ``dep_check`` (and optional ``projected`` cost dict); mutates
    nothing. See the module docstring for the invariants.
    """
    by_id = {s.run_id: s for s in program.runs}
    status = {rid: fleet_ledger.latest_status(rid) for rid in by_id}
    # blocker F5-2: an OPEN start-intent's run-status is "start-intent" (visible
    # now), so it is counted RUNNING -> its slot busy + its cost projected. The
    # running/done partition + the ceiling cost are over THIS program's runs (the
    # only runs we schedule and whose budgets we can project).
    running = {rid for rid, st in status.items() if st in ("running", "start-intent")}
    done = {rid for rid, st in status.items()
            if st in ("converged", "failed", "landed", "rejected")}
    # Busy slots, by contrast, are fleet-WIDE: a slot held by ANY running run
    # (this program's or another's) is unavailable. This program's own running
    # runs are included (they appear in the ledger run-status rows too).
    busy_slots = _occupied_slots(fleet_ledger)
    free_slots = [s for s in pool.slots if s not in busy_slots]
    ready = []
    for spec in program.runs:
        if spec.run_id in running or spec.run_id in done:
            continue
        if not all(dep_check(dep, spec.run_id) for dep in spec.depends_on):
            continue
        ready.append(spec)

    # ceiling: Sigma(running budgets) + this run's budget must stay within max_tokens.
    ceilinged = ceiling.max_tokens is not None
    used_tokens = sum(_project_or_inf(rid, projected, by_id, ceilinged)
                      for rid in running)
    startable: list[str] = []
    assignments: list = []
    used_slots = list(busy_slots)
    blocked_by_ceiling = False
    for spec in ready:
        slot = _pick_slot(spec, free_slots, pool, used_slots)
        cost = _project(spec.run_id, projected, by_id)
        # blocker F5-1: an UNBUDGETED run under a finite ceiling is UNKNOWN cost
        # -> fail closed (it WAITS), and that IS a ceiling block (so it can
        # surface Tier-1 even when ALSO slot-blocked -- major F5-allblocked).
        over_ceiling = ceilinged and (cost is _UNKNOWN_COST
                                      or used_tokens + cost > ceiling.max_tokens)
        over_runs = (ceiling.max_runs is not None
                     and len(used_slots) >= ceiling.max_runs)
        if over_ceiling or over_runs:
            blocked_by_ceiling = True          # set REGARDLESS of slot availability
            continue                           # would overspend / unknown -> WAIT
        if slot is None:
            continue                           # no free affinity-respecting slot
        startable.append(spec.run_id)
        assignments.append((spec.run_id, slot))
        # Only a KNOWN cost contributes to the running total; an unknown cost
        # only ever reaches here when there is no ceiling (over_ceiling already
        # blocks it otherwise), where the total is irrelevant -- adding the
        # sentinel would raise TypeError, so guard it.
        if cost is not _UNKNOWN_COST:
            used_tokens += cost
        used_slots.append(slot)
    # nothing could be scheduled AND a ceiling is implicated -> Tier-1 signal.
    all_blocked = (not startable) and blocked_by_ceiling and bool(ready)
    return ScheduleResult(startable=startable, assignments=assignments,
                          all_blocked_by_ceiling=all_blocked)
