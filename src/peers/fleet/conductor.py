"""STEP-6 -- the
deterministic, stateless, SINGLE-WRITER periodic conductor tick.

``conduct_tick`` supervises the WHOLE fleet (07 §7.5). It is the only writer of
the fleet-ledger; no other writer exists. Each tick, in order:

  0. an undeterminable dependency -- a TORN ``propagation-edge`` row (detected
     directly AND via the forced fail-closed parse's durable ``malformed-edge``
     marker; defense in depth, BUG-701) -- HALTS to Tier-2 before any scheduling.
  1. RECONCILE the observed world (``SlotRunner.observe()``) against the
     fleet-ledger PER SLOT -- an unaccounted run, a run on a slot the ledger never
     assigned (slot-mismatch), or the SAME run on two slots (double-occupancy)
     each HALT the whole tick (recorded durably, nothing else proceeds). A world
     the conductor disagrees with means its model of reality is wrong; scheduling
     on top of it could double-start or clobber (majors F4-slot-mismatch/-double).
  2. TRANSITION finished runs: a ``liveness=='wedged'`` run is restarted; a
     ``liveness=='done'`` run the ledger still calls ``running``/``start-intent``
     is re-verified on its OWN per-run ledger (``is_converged``, the fleet adds no
     new trust -- F2) and the conductor -- the SINGLE writer -- ``record_status``es
     it ``converged``/``failed`` ITSELF (blocker F4-status: without this the pool
     drains to zero as runs finish and routing never fires).
  3. ROUTE escalations: a CONVERGED run is re-checked self-hosting on its REAL
     converged diff (``_changed_paths(repo, base_sha, converged)``, NEVER
     ``changed_paths=None`` which forces 100% Tier-2 -- major F6-routing). A
     self-hosting run goes to Tier-2 (NEVER auto-lands -- §6.3; ``auto_merge.land``'s
     S4 re-detect is the SECOND, independent gate). A trusted converged
     ``auto-merge`` landing goes to the Tier-1 batched stack.
  4. RECORD DEPENDENCY-CONSUMPTION EDGES + WRITE-AHEAD START. For each scheduled
     consumer (the scheduler's costed ``(run_id, slot)`` assignments -- the
     conductor honors EXACTLY those, blocker F5-3) the conductor records a
     fleet-ledger ``propagation-edge`` per producer (intra AND cross, per-producer
     on fan-in, de-duplicated -- blockers F3-1/intra/fanin) BEFORE the consumer is
     started; the ``start-intent`` row is committed BEFORE ``SlotRunner.start`` so a
     crash between leaves an OPEN intent the next tick reconciles (write-ahead,
     F4). A consumer whose producer edge is UNRECORDABLE is surfaced to Tier-2 and
     NOT started (fail-closed: a started-but-edge-less consumer would silently
     strand a transitive dependent on a later rejection -- the cross-run
     self-greening F3 forbids).
  5. an ``all_blocked_by_ceiling`` schedule surfaces a Tier-1 batched decision.

``reject_run`` is the F3 consumer of ``cascade_invalidate``: revoke the transitive
closure, mark each ``rejected``, pull each from the in-flight Tier-1 stack, and
escalate an already-``landed`` revoked dependent to Tier-2 (a landed-then-rejected
run cannot be silently un-landed -- it is an honesty stop).

The injected boundaries (``slot_runner``/``ceiling``/``dep_check``/
``is_self_hosting``/``changed_paths_of``) keep the tick fully deterministic -- no
containers, no LLM. The only HARD spine imports (``self_hosting``,
``auto_merge._changed_paths``) are LAZY inside the ``_default_*`` defaults so this
module imports cleanly; production INJECTS ``is_self_hosting``/``changed_paths_of``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from peers.fleet.invalidate import cascade_invalidate
from peers.fleet.scheduler import startable_runs

# Statuses that mean a consumer has ALREADY consumed its producers' artifacts in
# a prior tick (it was started, which the scheduler permits only once every dep
# is satisfied). Its consumption edges may not have been recorded yet if the
# fleet-ledger was reconstructed mid-DAG (the e2e pre-seeds a converged mid-node),
# so the conductor records them here too -- idempotent (dedup) in a real run where
# the start tick already recorded them.
_CONSUMED_STATUSES = ("running", "converged", "landed")


def _default_self_hosting(repo, *, changed_paths, target_repo=None):
    """Production self-hosting detector (Stage-6, LAZY import -- so this module is
    import-safe even before Stage 6 lands; production injects this)."""
    from peers.spine.self_hosting import is_self_hosting

    return is_self_hosting(repo, changed_paths=changed_paths, target_repo=target_repo)


def _default_changed_paths(run, repo):
    """The REAL converged diff of ``run`` (``base_sha..converged``), or ``None``.

    ``None`` is UNDETERMINABLE -- ``is_self_hosting(None)`` fails SAFE (routes
    Tier-2), so the conductor can only OVER-route on uncertainty, never
    UNDER-route a real self-modification. Both spine imports are LAZY (only hit
    when a diff is actually computed)."""
    from peers.spine.propagate import _converged_commit       # Stage-0, always present

    converged = _converged_commit(run.ledger.read())
    base = getattr(run, "base_sha", None)
    if not base or converged is None:
        return None                                           # undeterminable -> fail safe
    from peers.spine.auto_merge import _changed_paths         # Stage-6, lazy

    return _changed_paths(repo, base, converged)


def _has_undeterminable_edge(fleet_ledger) -> bool:
    """True iff the fleet-ledger carries an undeterminable propagation dependency.

    Two INDEPENDENT signals (defense in depth -- never one safeguard; BUG-701):

      1. a raw TORN ``propagation-edge`` row (missing/empty ``from_run`` or
         ``to_run``, or a non-dict witness) parsed DIRECTLY here -- so the halt does
         not depend on the side-effecting lazy flag at all; and
      2. a durable ``malformed-edge`` marker a PRIOR parse already recorded
         (preserves the original marker-based safeguard).

    ``propagation_edges()`` is invoked FIRST to force the substrate's canonical
    fail-closed parse, which records a durable ``malformed-edge`` marker for any
    torn row. The bug: ``conduct_tick`` step 0 checked ONLY for signal (2), but
    that marker is written lazily by ``propagation_edges()`` -- which a root-only
    tick (no consumer with producers to record) never calls -- so a torn row no
    prior tick had parsed slipped through step 0 and work was scheduled OVER it.
    Forcing the parse + scanning the raw rows directly closes both gaps.
    """
    fleet_ledger.propagation_edges()                 # force the fail-closed parse (durable marker)
    rows = fleet_ledger.read()
    torn = any(
        r.event == "propagation-edge"
        and not (isinstance(r.witness, dict)
                 and r.witness.get("from_run") and r.witness.get("to_run"))
        for r in rows)
    marker = any(r.event == "malformed-edge" for r in rows)
    return torn or marker


def _converged_tip(run) -> str | None:
    """The producer's ledger-bound attested CONVERGED sha (the same tip
    ``propagate_branch`` ships; an intra-tool producer gets this too). ``None``
    when the run is unbound or not converged on its own ledger."""
    if run is None:
        return None
    from peers.spine.propagate import _converged_commit

    return _converged_commit(run.ledger.read())


def _artifact_for(spec) -> str:
    """The branch artifact name recorded on the edge witness (mirrors
    ``propagate_branch``'s ``artifact``)."""
    return f"peers/run/{spec.run_id}" if spec is not None else "?"


def _producer_edges(consumer_id, *, by_id, runs_by_id, repos_by_id):
    """Resolve the ``(from, to, artifact, repo, tip, is_intra)`` edge tuple for
    EVERY producer of ``consumer_id`` (every id in ``depends_on`` -- a consumer
    that is consuming/has consumed did so for ALL its declared producers).

    Returns ``(edges, unrecordable)``: ``edges`` are the fully-resolved tuples;
    ``unrecordable`` is the first dep whose producer edge could NOT be resolved
    (a scheduler/world divergence on a CROSS-tool edge that genuinely needs the
    propagatable git-sha), or ``None`` if all resolved. The caller fails CLOSED
    on a non-None ``unrecordable`` when STARTING a consumer.

    P0-a (review finding): an INTRA-tool producer with a non-git-sha converged
    artifact (research file / find-bugs finding -> ``_converged_commit`` is
    ``None``) is NOT unrecordable -- the scheduler already verified the dep
    (``dep_satisfied``'s intra branch needs only ``is_converged``, never a
    git-sha), so we mark it for a non-attested cascade edge instead of stranding
    a valid consumer. The intra/cross discriminator is the SPEC TOOL identity
    (lock-step stable), NEVER ``repos_by_id`` -- under Stage-5 isolation
    ``repos_by_id`` maps each run to its OWN leased worktree, so two runs of the
    SAME tool would look cross and the intra consumer would be re-stranded."""
    spec = by_id[consumer_id]
    consumer_tool = Path(spec.tool).resolve()
    edges: list[tuple[str, str, str, object, str | None, bool]] = []
    unrecordable = None
    for dep in spec.depends_on:
        dep_spec = by_id.get(dep)
        producer = runs_by_id.get(dep)
        prod_repo = repos_by_id.get(dep, dep_spec.tool if dep_spec is not None else None)
        is_intra = (dep_spec is not None
                    and Path(dep_spec.tool).resolve() == consumer_tool)
        tip = _converged_tip(producer)
        if tip is None and is_intra:
            # intra-tool + non-git-sha converged artifact -> non-attested cascade
            # edge (the cascade only needs from/to; prod_repo is unused for it).
            edges.append((dep, consumer_id, _artifact_for(dep_spec), None, None, True))
            continue
        if tip is None or prod_repo is None:
            unrecordable = dep                       # cross-tool needs the propagatable sha
            continue
        edges.append((dep, consumer_id, _artifact_for(dep_spec), prod_repo, tip, is_intra))
    return edges, unrecordable


def _record_edge(fleet_ledger, frm, to, art, prepo, tip, is_intra) -> None:
    """Record one resolved producer edge: a non-attested INTRA cascade edge when
    the intra producer has no git-sha (``tip is None``), else the attested
    git-sha ``propagation-edge`` (cross-tool, or intra with a real sha)."""
    if tip is None and is_intra:
        fleet_ledger.record_intra_edge(frm, to, art)
    else:
        fleet_ledger.record_propagation_edge(frm, to, art, repo=prepo, tip_sha=tip)


@dataclass
class ConductorReport:
    """What one tick decided. List defaults are factories (NO mutable scalar
    default); ``halted``/``reason`` are scalars."""

    started: list = field(default_factory=list)
    restarted: list = field(default_factory=list)
    converged: list = field(default_factory=list)
    tier1: list = field(default_factory=list)
    tier2: list = field(default_factory=list)
    halted: bool = False
    reason: str = ""


def conduct_tick(fleet_ledger, program, pool, *, slot_runner, ceiling, dep_check,
                 is_self_hosting=_default_self_hosting, changed_paths_of=None,
                 runs_by_id=None, repos_by_id=None, projected=None) -> ConductorReport:
    """Run one deterministic, single-writer conductor tick over ``fleet_ledger``.

    See the module docstring for the per-tick order (reconcile-or-halt =>
    liveness transition => routing => edge recording + write-ahead start =>
    ceiling). Returns a :class:`ConductorReport`. Nothing else proceeds on a
    reconcile HALT."""
    rep = ConductorReport()
    by_id = {s.run_id: s for s in program.runs}
    runs_by_id = runs_by_id or {}
    repos_by_id = repos_by_id or {s.run_id: s.tool for s in program.runs}
    observed = slot_runner.observe()              # {slot: run_id|None}

    # 0. an undeterminable dependency HALTs to Tier-2 BEFORE any scheduling. We FORCE
    #    the fail-closed parse AND scan the raw rows directly (two independent layers --
    #    a torn propagation-edge row that no prior tick parsed otherwise slips
    #    through, because the malformed-edge marker is written lazily by
    #    propagation_edges(), which a root-only tick never calls).
    if _has_undeterminable_edge(fleet_ledger):
        reason = "malformed-edge: undeterminable dependency"
        fleet_ledger.record_halt(reason)
        rep.halted = True
        rep.reason = reason
        return rep

    # 1. RECONCILE-OR-HALT (PER SLOT): map observed runs -> their slot(s) and
    #    check (a) double-occupancy (one run on two slots), (b) per-slot mismatch
    #    (the ledger assigned a DIFFERENT slot), (c) unaccounted (no intent/status).
    run_to_slots: dict[str, list[str]] = {}
    for slot, run_id in observed.items():
        if run_id is not None:
            run_to_slots.setdefault(run_id, []).append(slot)
    for run_id, slots in run_to_slots.items():
        if len(slots) > 1:                        # major F4-double-start
            reason = f"double-occupancy: run {run_id!r} on slots {sorted(slots)}"
            fleet_ledger.record_halt(reason)
            rep.halted = True
            rep.reason = reason
            return rep
        slot = slots[0]
        ledger_slot = fleet_ledger.slot_of(run_id)
        intent_slots = [s for (rid, s) in fleet_ledger.intents() if rid == run_id]
        known = (run_id in by_id and fleet_ledger.latest_status(run_id) in
                 ("running", "start-intent", "converged", "failed", "landed", "rejected")) \
            or intent_slots
        if not known:                             # unaccounted
            reason = f"world-divergence: slot {slot} runs unaccounted {run_id!r}"
            fleet_ledger.record_halt(reason)
            rep.halted = True
            rep.reason = reason
            return rep
        assigned = ledger_slot or (intent_slots[0] if intent_slots else None)
        if assigned is not None and assigned != slot:   # major F4-slot-mismatch
            reason = f"slot-mismatch: run {run_id!r} on {slot}, ledger assigned {assigned}"
            fleet_ledger.record_halt(reason)
            rep.halted = True
            rep.reason = reason
            return rep

    # 2. liveness reconcile: wedged -> restart; done -> the conductor (single
    #    writer) transitions the run to a TERMINAL fleet status itself.
    for slot, run_id in observed.items():
        if run_id is None:
            continue
        live = slot_runner.liveness(run_id)
        if live == "wedged":
            slot_runner.start(slot, by_id[run_id])
            fleet_ledger.record_status(run_id, "running", slot=slot)
            rep.restarted.append(run_id)
        elif live == "done" and fleet_ledger.latest_status(run_id) in (
                "running", "start-intent"):
            # re-verify CONVERGED on the run's OWN per-run ledger.
            run = runs_by_id.get(run_id)
            ok = False
            if run is not None:
                from peers.spine.propagate import is_converged
                # HONEST-01: anchor attest-reachability on the run's tip (its
                # branch, or — post-teardown — its pinned ref exposed via run.branch
                # by the SlotRunner), NOT the parent repo HEAD (where the run's
                # branch commits are not reachable).
                ok = is_converged(run.ledger.read(), mode_run=run.mode_run,
                                  repo=repos_by_id.get(run_id, by_id[run_id].tool),
                                  head=getattr(run, "branch", None) or "HEAD")
            fleet_ledger.record_status(run_id, "converged" if ok else "failed")
            if ok:
                rep.converged.append(run_id)

    # 3. ROUTING: a CONVERGED run is re-checked self-hosting on its REAL
    #    converged diff -- never changed_paths=None (which forces 100% Tier-2).
    cp_of = changed_paths_of or (
        lambda rid: _default_changed_paths(
            runs_by_id[rid], repos_by_id.get(rid, by_id[rid].tool))
        if rid in runs_by_id else None)
    for spec in program.runs:
        if fleet_ledger.latest_status(spec.run_id) != "converged":
            continue
        repo = repos_by_id.get(spec.run_id, spec.tool)
        hosting, _why = is_self_hosting(repo, changed_paths=cp_of(spec.run_id),
                                        target_repo=repo)
        if hosting:                               # §6.3: forced to the blocking surface
            rep.tier2.append(spec.run_id)
        elif spec.op_config.landing == "auto-merge":
            rep.tier1.append(spec.run_id)         # batched landing (land() re-detects S4)

    # 4. F3 EDGE RECORDING + WRITE-AHEAD START.
    sched = startable_runs(program, fleet_ledger, pool, ceiling,
                           dep_check=dep_check, projected=projected)
    starting = {run_id for run_id, _slot in sched.assignments}

    # 4a. CONSUMED-IN-A-PRIOR-TICK consumers: a run already running/converged/landed
    #     consumed ALL its declared producers; record those edges so the cascade
    #     graph is COMPLETE even for a mid-DAG node that converged before this tick
    #     (the e2e compresses ticks by pre-seeding a converged consumer whose start
    #     tick -- and thus its edge recording -- never ran). Best-effort: an
    #     unresolvable producer is skipped here (the run already ran; it cannot be
    #     un-started). Idempotent in a real run (the start tick already recorded it).
    for spec in program.runs:
        if spec.run_id in starting or not spec.depends_on:
            continue
        if fleet_ledger.latest_status(spec.run_id) not in _CONSUMED_STATUSES:
            continue
        edges, _unrecordable = _producer_edges(
            spec.run_id, by_id=by_id, runs_by_id=runs_by_id, repos_by_id=repos_by_id)
        for (frm, to, art, prepo, tip, is_intra) in edges:
            _record_edge(fleet_ledger, frm, to, art, prepo, tip, is_intra)

    # 4b. STARTING consumers: record EVERY producer edge FIRST (fail-closed -- an
    #     unrecordable edge surfaces Tier-2 and does NOT start the consumer), then
    #     write-ahead start on the EXACT slot the scheduler costed (blocker F5-3).
    for run_id, slot in sched.assignments:
        spec = by_id[run_id]
        edges, unrecordable = _producer_edges(
            run_id, by_id=by_id, runs_by_id=runs_by_id, repos_by_id=repos_by_id)
        if unrecordable is not None:              # fail-closed: surface, do NOT start
            rep.tier2.append(f"unrecordable-edge {unrecordable}->{run_id}")
            continue
        for (frm, to, art, prepo, tip, is_intra) in edges:   # all resolvable -> record EVERY edge
            _record_edge(fleet_ledger, frm, to, art, prepo, tip, is_intra)
        fleet_ledger.record_start_intent(run_id, slot)   # (1) WRITE-AHEAD (visible to scheduler)
        slot_runner.start(slot, spec)                    # (2) then start on the COSTED slot
        fleet_ledger.record_status(run_id, "running", slot=slot)
        rep.started.append(run_id)

    # 5. -> Tier-1 batched ceiling decision.
    if sched.all_blocked_by_ceiling:
        rep.tier1.append("ceiling: raise-or-stop")
    return rep


def reject_run(fleet_ledger, run_id, reason, *, program, tier1_stack=None) -> ConductorReport:
    """F3 consumer (major F3-reject + F4-reschedule): revoke ``run_id`` and its
    transitive dependents.

    Marks ``run_id`` + the full forward closure ``rejected`` (a non-converged
    rejected run is reschedulable), pulls each revoked dependent from the in-flight
    Tier-1 landing stack, and escalates an already-``landed`` revoked dependent to
    Tier-2 (a landed-then-rejected run cannot be silently un-landed -- an honesty
    stop). The repair-reconverge half (``supersede_edge`` the stale edges + reset
    the dependents to ``pending`` so they re-schedule against the corrected
    artifact) is driven by the daemon loop after the producer re-converges; this
    proves the mark-reject + pull-from-Tier-1 + landed-escalation."""
    rep = ConductorReport()
    tier1_stack = list(tier1_stack or [])
    # P0-b (review finding): capture the ROOT's prior status BEFORE overwriting it.
    # The root run is handled separately from the cascade closure (which EXCLUDES
    # it), and the old code marked it 'rejected' without this check -- so a
    # landed-then-rejected ROOT was silently un-landed with no honesty stop,
    # asymmetric with its landed dependents below.
    was_root = fleet_ledger.latest_status(run_id)
    revoked = cascade_invalidate(fleet_ledger, run_id)           # the transitive closure
    fleet_ledger.record_status(run_id, "rejected")
    if run_id in tier1_stack:
        tier1_stack.remove(run_id)                               # pull the root's own landing
    if was_root == "landed":
        rep.tier2.append(run_id)                                 # landed-then-rejected ROOT -> Tier-2
    for r in revoked:
        was = fleet_ledger.latest_status(r)
        fleet_ledger.record_status(r, "rejected")                # non-converged -> reschedulable
        if r in tier1_stack:
            tier1_stack.remove(r)                                # pull its landing from Tier-1
        if was == "landed":
            rep.tier2.append(r)                                  # landed-then-rejected -> Tier-2
    rep.tier1 = tier1_stack
    return rep
