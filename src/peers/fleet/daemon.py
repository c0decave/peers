"""``conduct_fleet`` — the production caller of the conductor tick.

The fleet library (``conduct_tick``/``reject_run``/scheduler/satisfy) is a
single deterministic step; this is the loop that drives it to an HONEST terminal.
Each tick it assembles run objects from the SlotRunner, ticks the conductor,
LANDS converged auto-merge runs (``auto_merge.land`` is the second, independent
§6.3 gate — NEVER a self-hosting run), surfaces Tier-2, optionally drives the
``reject_run`` cascade (a post-convergence skeptic / external invalidation), and
stops with the REAL cause:

  * ``complete``  — every run reached a terminal status.
  * ``halted``    — a reconcile / malformed-edge HALT (Tier-2).
  * ``ceiling``   — all ready work blocked by the aggregate cap, nothing in flight.
  * ``stalled``   — nothing running, nothing startable, not all terminal (a failed
                    producer stranded its consumers, or the cross-tool seam is
                    unwired). Surfaced, never spun on.
  * ``max-ticks`` — tick budget exhausted (incomplete).
  * ``aborted``   — an unexpected error; an honest terminal (mirrors cmd_bring_up).

Single-threaded, so the conductor stays the single per-tick fleet-ledger writer
(this loop's only extra writes are ``landed`` after a successful ``land`` and the
``reject_run`` cascade — sequential, no concurrent writer). All boundaries are
injected (``slot_runner``/``dep_check``/``is_self_hosting``/``lander``/``sleep``)
so the loop is deterministic in tests. See
``docs/plans/2026-06-13-fleet-daemon-design.md``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from peers.fleet.conductor import _default_self_hosting, conduct_tick, reject_run
from peers.fleet.satisfy import make_dep_check

#: Statuses a run can no longer make automatic progress past (converged = the
#: fleet's automatic job is done: it is landed, surfaced Tier-2, or a manual
#: branch-pr awaits a human).
_TERMINAL = frozenset({"landed", "failed", "rejected", "converged"})
#: Statuses that count a run as in-flight (the loop keeps ticking while any live).
_IN_FLIGHT = frozenset({"running", "start-intent"})


@dataclass
class FleetResult:
    """What a whole ``conduct_fleet`` run decided (honest terminal)."""

    cause: str
    ticks: int
    statuses: dict
    landed: list = field(default_factory=list)
    tier2: list = field(default_factory=list)
    needs_review: dict = field(default_factory=dict)
    halt_reason: str = ""
    error: str = ""
    ok: bool = False


def _default_lander(run, *, repo, target_ref):
    """Fail-closed default: auto-landing requires an injected ``recheck`` (the
    second independent §6.3 gate on a fresh worktree). Without one we do NOT land;
    the converged run is surfaced for review rather than landed on trust."""
    from peers.spine.auto_merge import LandingResult

    return LandingResult(
        landed=False, reason="auto-land recheck not wired (inject a lander)")


def _finish(cause, ticks, statuses, landed, tier2, needs_review, *,
            halt_reason="", error="") -> FleetResult:
    failed = any(s in ("failed", "rejected") for s in statuses.values())
    ok = (cause == "complete" and not failed and not tier2 and not needs_review)
    return FleetResult(
        cause=cause, ticks=ticks, statuses=dict(statuses), landed=list(landed),
        tier2=list(tier2), needs_review=dict(needs_review),
        halt_reason=halt_reason, error=error, ok=ok)


def conduct_fleet(fleet_ledger, program, pool, ceiling, *, slot_runner,
                  repos_by_id, dep_check=None, is_self_hosting=_default_self_hosting,
                  changed_paths_of=None, lander=None, should_reject=None,
                  target_ref="main", max_ticks=None, tick_sleep_s=5.0,
                  projected=None, sleep=None, on_report=None) -> FleetResult:
    """Drive ``program`` to an honest terminal; return a :class:`FleetResult`."""
    by_id = {s.run_id: s for s in program.runs}
    lander = lander or _default_lander
    sleep = sleep or time.sleep
    landed: list = []
    needs_review: dict = {}
    tier2_seen: list = []
    ticks = 0

    def _statuses():
        return {rid: fleet_ledger.latest_status(rid) for rid in by_id}

    while True:
        ticks += 1
        try:
            runs_by_id = (slot_runner.runs_by_id()
                          if hasattr(slot_runner, "runs_by_id") else {})
            dc = dep_check or make_dep_check(by_id, runs_by_id, repos_by_id)
            rep = conduct_tick(
                fleet_ledger, program, pool, slot_runner=slot_runner,
                ceiling=ceiling, dep_check=dc, is_self_hosting=is_self_hosting,
                changed_paths_of=changed_paths_of, runs_by_id=runs_by_id,
                repos_by_id=repos_by_id, projected=projected)
            if on_report is not None:
                on_report(ticks, rep)

            if rep.halted:
                return _finish("halted", ticks, _statuses(), landed, tier2_seen,
                               needs_review, halt_reason=rep.reason)

            _land_tier1(rep, by_id, runs_by_id, repos_by_id, fleet_ledger,
                        lander, target_ref, landed, needs_review)

            for entry in rep.tier2:
                if entry not in tier2_seen:
                    tier2_seen.append(entry)

            if should_reject is not None:
                _drive_rejections(program, fleet_ledger, should_reject, tier2_seen)

            statuses = _statuses()
            if all(s in _TERMINAL for s in statuses.values()):
                return _finish("complete", ticks, statuses, landed, tier2_seen,
                               needs_review)

            ceiling_block = any("ceiling" in str(t) for t in rep.tier1)
            in_flight = any(s in _IN_FLIGHT for s in statuses.values())
            progressed = bool(rep.started or rep.restarted)
            if ceiling_block and not in_flight:
                return _finish("ceiling", ticks, statuses, landed, tier2_seen,
                               needs_review)
            if not in_flight and not progressed:
                return _finish("stalled", ticks, statuses, landed, tier2_seen,
                               needs_review)
            if max_ticks is not None and ticks >= max_ticks:
                return _finish("max-ticks", ticks, statuses, landed, tier2_seen,
                               needs_review)
        except Exception as e:                       # noqa: BLE001 — honest terminal
            return _finish("aborted", ticks, _statuses(), landed, tier2_seen,
                           needs_review, error=f"{type(e).__name__}: {e}")
        sleep(tick_sleep_s)


def _land_tier1(rep, by_id, runs_by_id, repos_by_id, fleet_ledger, lander,
                target_ref, landed, needs_review) -> None:
    """Land each converged auto-merge run the conductor routed to Tier-1. Landing
    is per-run fail-closed: a lander error / refusal surfaces the run for review,
    NEVER aborts the fleet and NEVER records ``landed`` on trust."""
    for entry in rep.tier1:
        spec = by_id.get(entry)
        if (spec is None or spec.op_config.landing != "auto-merge"
                or fleet_ledger.latest_status(entry) != "converged"):
            continue                                 # the "ceiling: ..." sentinel etc.
        run = runs_by_id.get(entry)
        res = None
        if run is not None:
            try:
                res = lander(run, repo=repos_by_id.get(entry, spec.tool),
                             target_ref=target_ref)
            except Exception as e:                   # noqa: BLE001 — per-run fail-closed
                needs_review[entry] = f"land error: {e}"
                continue
        if res is not None and res.landed:
            fleet_ledger.record_status(entry, "landed")
            landed.append(entry)
        else:
            needs_review[entry] = (
                res.reason if res is not None else "no run object for landing")


def _drive_rejections(program, fleet_ledger, should_reject, tier2_seen) -> None:
    """Apply the post-convergence reject seam: a ``should_reject`` verdict on a
    converged run drives the full ``reject_run`` cascade (revoke transitive
    dependents; a landed-then-rejected dependent escalates to Tier-2)."""
    for spec in list(program.runs):
        if fleet_ledger.latest_status(spec.run_id) != "converged":
            continue
        reason = should_reject(spec.run_id, fleet_ledger)
        if reason:
            rj = reject_run(fleet_ledger, spec.run_id, reason, program=program)
            for t in rj.tier2:
                if t not in tier2_seen:
                    tier2_seen.append(t)
