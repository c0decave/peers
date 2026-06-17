# tests/unit/test_fleet_scheduler.py
"""STEP-4 -- the pure pool/ceiling scheduler.

The first block is the canonical fail-first contract from
docs/plans/2026-06-11-agentic-os-stage-7.md (STEP-4). The second block adds
happy / edge / sad coverage the canonical names do not exercise -- and hunts
adjacent bugs the contract list omits: the ``max_runs`` ceiling boundary, an
``unknown`` affinity label, that two ready runs never get assigned the SAME
slot, and the empty program.
"""
from tests.unit._fleet_helpers import _spec, _program, _fleet_ledger
from tests.unit._isolation_helpers import _init_repo

from peers.fleet.scheduler import startable_runs, Pool, Ceiling, ScheduleResult


def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    return p


def _always(ok):
    return lambda producer_id, consumer_id: ok       # injected dep_check stub


# --------------------------------------------------------------------------
# Canonical contract (docs/plans/2026-06-11-agentic-os-stage-7.md STEP-4)
# --------------------------------------------------------------------------
def test_root_runs_are_startable_with_free_slots(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x.parent))  # no deps
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(),
                         dep_check=_always(True))
    assert set(res.startable) == {"a", "b"} and res.all_blocked_by_ceiling is False


def test_dependent_not_startable_until_dep_satisfied(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x, depends_on=["a"]))
    fl = _fleet_ledger(tmp_path)
    # a is converged in the ledger but dep_check is the authority: deny -> b waits
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(),
                         dep_check=_always(False))
    assert "b" not in res.startable


def test_running_runs_are_not_rescheduled(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")        # a already running on s0
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(),
                         dep_check=_always(True))
    assert "a" not in res.startable and "b" in res.startable


def test_no_free_slot_blocks_scheduling(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")        # the only slot is taken
    res = startable_runs(prog, fl, Pool(slots=["s0"]), Ceiling(), dep_check=_always(True))
    assert res.startable == []                         # no free slot -> nothing startable


def test_affinity_pins_a_run_to_its_slot(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, affinity="bigmem"))
    fl = _fleet_ledger(tmp_path)
    # pool maps the affinity label to a specific slot; only that slot satisfies it
    pool = Pool(slots=["s0", "s1"], affinity={"bigmem": "s1"})
    res = startable_runs(prog, fl, pool, Ceiling(), dep_check=_always(True))
    assert "a" in res.startable                        # s1 is free and matches affinity
    fl.record_status("z", "running", slot="s1")        # now occupy the affinity slot
    res2 = startable_runs(prog, fl, pool, Ceiling(), dep_check=_always(True))
    assert "a" not in res2.startable                   # no other slot satisfies the pin


def test_ceiling_blocks_a_run_that_would_overspend(tmp_path):
    x = _repo(tmp_path, "x")
    # each run budgets 100 tokens; a running run already consumes 80 of a 150 ceiling.
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]),
                         Ceiling(max_tokens=150),
                         dep_check=_always(True),
                         projected={"a": 80, "b": 100})  # injected projector
    assert "b" not in res.startable                    # 80 + 100 > 150 -> b waits
    assert res.all_blocked_by_ceiling is True          # nothing else can progress -> Tier-1


def test_ceiling_none_means_no_aggregate_cap(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(max_tokens=None),
                         dep_check=_always(True))
    assert set(res.startable) == {"a", "b"}            # explicit opt-out, not silent infinity


def test_ceiling_blocks_via_REAL_op_config_budget_no_injected_projector(tmp_path):
    # blocker F5-1: the PRODUCTION path (NO injected `projected`) must bound the
    # ceiling using the real op_config.budget.max_tokens -- not project 0 (which
    # silently overspends). a (100) running consumes 100 of a 150 ceiling; b (100)
    # would cross -> blocked.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, max_tokens=100),
                    _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(max_tokens=150),
                         dep_check=_always(True))        # NO projected= -> real budget
    assert "b" not in res.startable
    assert res.all_blocked_by_ceiling is True


def test_unbudgeted_run_under_a_ceiling_fails_closed(tmp_path):
    # blocker F5-1: a run with NO per-run budget under a non-None ceiling is UNKNOWN
    # cost -> it WAITS (never assumed free / never overspends).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))                  # max_tokens unset -> None
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0"]), Ceiling(max_tokens=1000),
                         dep_check=_always(True))         # no projector
    assert res.startable == []                            # unbudgeted-under-ceiling -> waits
    assert res.all_blocked_by_ceiling is True


def test_unbudgeted_run_with_no_ceiling_is_fine(tmp_path):
    # the opt-out: NO ceiling -> an unbudgeted run is startable (no aggregate cap).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0"]), Ceiling(max_tokens=None),
                         dep_check=_always(True))
    assert "a" in res.startable


def test_open_start_intent_occupies_slot_and_projects_cost(tmp_path):
    # blocker F5-2: an OPEN start-intent (no running status yet) makes its slot busy
    # AND its budget count toward used_tokens on the next schedule.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, max_tokens=100),
                    _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")                    # OPEN intent on s0
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(max_tokens=150),
                         dep_check=_always(True))
    assert "a" not in res.startable                      # a is the intent (already in flight)
    assert "b" not in res.startable                      # 100 (a) + 100 (b) > 150 -> blocked
    assert res.all_blocked_by_ceiling is True


def test_all_blocked_when_slot_scarce_and_ceiling_pending(tmp_path):
    # major F5-allblocked: the ONLY runnable work is over-ceiling AND slots are full ->
    # all_blocked_by_ceiling must STILL be True (so the conductor surfaces Tier-1, never
    # a silent stall). Previously _pick_slot's `continue` masked the ceiling reason.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, max_tokens=80),
                    _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")          # the only slot is taken
    res = startable_runs(prog, fl, Pool(slots=["s0"]), Ceiling(max_tokens=150),
                         dep_check=_always(True))
    assert res.startable == []
    assert res.all_blocked_by_ceiling is True             # ceiling reason surfaced despite no slot


def test_assignments_carry_run_id_slot_pairs(tmp_path):
    # blocker F5-3: the scheduler returns the (run_id, slot) PAIRS it costed; the
    # conductor honors exactly these (never re-derives its own free slots).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, affinity="bigmem"))
    pool = Pool(slots=["s0", "s1"], affinity={"bigmem": "s1"})
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, pool, Ceiling(), dep_check=_always(True))
    assert ("a", "s1") in res.assignments                 # a was costed onto its affinity slot


# --------------------------------------------------------------------------
# Added coverage (happy / edge / sad) + adjacent-bug hunting
# --------------------------------------------------------------------------
def test_single_root_run_is_startable_and_assigned(tmp_path):
    # happy: the minimal nominal case -- one root run, one free slot, default ceiling.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0"]), Ceiling(), dep_check=_always(True))
    assert isinstance(res, ScheduleResult)                # the documented return type
    assert res.startable == ["a"]
    assert res.assignments == [("a", "s0")]
    assert res.all_blocked_by_ceiling is False


def test_empty_program_schedules_nothing(tmp_path):
    # edge: an empty program has no ready runs -> nothing startable, not a ceiling block.
    prog = _program()                                     # zero runs
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(max_tokens=100),
                         dep_check=_always(True))
    assert res.startable == []
    assert res.assignments == []
    assert res.all_blocked_by_ceiling is False            # no ready runs -> not a Tier-1 signal


def test_max_runs_ceiling_at_boundary_blocks_extra_start(tmp_path):
    # edge: the max_runs axis of the ceiling (the contract has NO canonical test for it).
    # one run already occupies a slot; max_runs=1 means at the boundary -> no new start,
    # and that IS a ceiling block (Tier-1), independent of token budget.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "running", slot="s0")
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(max_runs=1),
                         dep_check=_always(True))
    assert "b" not in res.startable                       # already at the run cap
    assert res.all_blocked_by_ceiling is True


def test_multiple_ready_runs_never_share_one_slot(tmp_path):
    # edge: two ready runs with two free slots must land on DISTINCT slots -- a regression
    # guard for used_slots not being threaded (else both would be costed onto s0).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x))
    fl = _fleet_ledger(tmp_path)
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(), dep_check=_always(True))
    assert set(res.startable) == {"a", "b"}
    assigned_slots = [slot for _rid, slot in res.assignments]
    assert sorted(assigned_slots) == ["s0", "s1"]         # distinct slots, no double-assign


def test_unknown_affinity_label_makes_a_run_unschedulable(tmp_path):
    # sad: an affinity label the pool does not map is UNSATISFIABLE -> the run waits, and
    # this is a slot/affinity block (NOT a ceiling block -> all_blocked stays False).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, affinity="gpu"))
    fl = _fleet_ledger(tmp_path)
    pool = Pool(slots=["s0", "s1"], affinity={})          # no "gpu" mapping
    res = startable_runs(prog, fl, pool, Ceiling(), dep_check=_always(True))
    assert "a" not in res.startable
    assert res.assignments == []
    assert res.all_blocked_by_ceiling is False            # blocked by affinity, not the ceiling
