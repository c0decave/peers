"""STEP-6 -- conductor.

Host-only + deterministic: REAL tmp git repos via subprocess + the injected
``FakeSlotRunner`` (no containers, no subprocess peers, no network/LLM). The
conductor is the SINGLE writer of the fleet-ledger; these tests pin its
write-ahead start ordering, the per-slot reconcile-or-halt, the single-writer
terminal-status transition, the dependency-consumption EDGE recording the cascade
walks, the self-hosting routing, and ``reject_run``'s cascade.
"""
from tests.unit._fleet_helpers import _spec, _program, _fleet_ledger, FakeSlotRunner
from tests.unit._isolation_helpers import (_init_repo, _attested_repo,
                                          _commit_on_branch, _run,
                                          _converged_branch_ledger)

from peers.fleet.conductor import conduct_tick, reject_run
from peers.fleet.scheduler import Pool, Ceiling
from peers.fleet.invalidate import cascade_invalidate


def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    return p


def _trusted(repo, **kw):
    return (False, "")                                 # injected: nothing is self-hosting


def _self_hosting(repo, **kw):
    return (True, "target-is-peers")                   # injected: everything is self-hosting


def test_write_ahead_start_on_costed_slot(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    sr = FakeSlotRunner(slots=["s0"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert "a" in rep.started
    assert fl.latest_status("a") in ("running", "start-intent")
    assert ("s0", "a") in sr.started                    # started on the scheduler's costed slot


def _converged_run(repo, mode_run, peer="claude"):
    tip = _commit_on_branch(repo, f"peers/run/{mode_run}", f"{mode_run}.py", "x", peer=peer)
    r = _run(repo, mode_run=mode_run, tool=repo, branch=f"peers/run/{mode_run}")
    r._ledger = _converged_branch_ledger(repo, repo / f"{mode_run}.jsonl", mode_run, tip)
    return r


def test_conductor_records_consumption_edges_for_every_producer(tmp_path):
    # blockers F3-1 / F3-intra / F3-fanin: the conductor records a fleet-ledger edge
    # for EVERY satisfied (producer, consumer) pair when the consumer is scheduled --
    # intra-tool too, per-producer on fan-in -- so the cascade has a graph to walk.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    # diamond fan-in: a->b, a->c, b&c -> d ; b,c CONVERGED producer runs, d about to start
    prog = _program(
        _spec("a", tool=x), _spec("b", tool=x, depends_on=["a"]),
        _spec("c", tool=x, depends_on=["a"]),
        _spec("d", tool=x, depends_on=["b", "c"]))
    fl = _fleet_ledger(x)
    runs = {rid: _converged_run(x, rid) for rid in ("a", "b", "c")}
    for rid in ("a", "b", "c"):
        fl.record_status(rid, "converged")
    sr = FakeSlotRunner(slots=["s0", "s1", "s2", "s3"])
    conduct_tick(fl, prog, Pool(slots=["s0", "s1", "s2", "s3"]), slot_runner=sr,
                 ceiling=Ceiling(), dep_check=lambda p, c: True, is_self_hosting=_trusted,
                 runs_by_id=runs, repos_by_id={rid: x for rid in ("a", "b", "c", "d")})
    # d consumed BOTH b and c -> BOTH edges recorded by the conductor (NOT the test)
    edges = {(f, t) for f, t, _a in fl.propagation_edges()}
    assert ("b", "d") in edges and ("c", "d") in edges
    # rejecting c alone (not via a) revokes d through the c->d edge the conductor made
    assert cascade_invalidate(fl, "c") == {"d"}


def test_intra_tool_dependent_is_cascaded_through_conductor_edges(tmp_path):
    # major F3-intra: fbX -> devX is INTRA-tool; the conductor still records the edge so
    # rejecting fbX revokes devX (intra consumption is an edge too).
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    prog = _program(_spec("fbX", tool=x, mode="find-bugs:reproduce"),
                    _spec("devX", tool=x, mode="develop", depends_on=["fbX"]))
    fl = _fleet_ledger(x)
    fbX = _converged_run(x, "fbX")
    fl.record_status("fbX", "converged")
    sr = FakeSlotRunner(slots=["s0", "s1"])
    conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr, ceiling=Ceiling(),
                 dep_check=lambda p, c: True, is_self_hosting=_trusted,
                 runs_by_id={"fbX": fbX}, repos_by_id={"fbX": x, "devX": x})
    assert ("fbX", "devX") in {(f, t) for f, t, _a in fl.propagation_edges()}
    assert cascade_invalidate(fl, "fbX") == {"devX"}


def test_world_divergence_halts_to_tier2(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "intruder"})   # unknown run on s0
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and "divergence" in rep.reason
    assert rep.started == []
    assert [r for r in fl.read() if r.event == "halt"]


def test_unparsed_malformed_edge_halts_before_scheduling(tmp_path):
    # BUG-701 (high, fix_by claude) / Concern 5: a TORN propagation-edge row
    # (from_run + artifact, NO to_run) is an undeterminable dependency. conduct_tick
    # used to check ONLY for a pre-existing malformed-edge MARKER, but that marker is
    # written LAZILY by propagation_edges(), which a root-only tick never calls -- so
    # the torn row slipped through step 0 and work was scheduled OVER it (observed:
    # halted False, start-intent/running appended). The conductor must FORCE the
    # fail-closed parse and HALT to Tier-2 before starting anything.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))                  # a schedulable root run
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="propagation-edge", status="ok", subject="p->",
                   witness={"from_run": "p", "artifact": "peers/run/p"})   # NO to_run
    sr = FakeSlotRunner(slots=["s0"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and "malformed" in rep.reason
    assert rep.started == []
    assert sr.started == []                              # NOTHING started on any slot
    # nothing scheduled over the torn edge: 'a' never reached start-intent/running
    assert fl.latest_status("a") not in ("start-intent", "running")
    assert [r for r in fl.read() if r.event == "halt"]   # the Tier-2 halt is recorded


def test_torn_edge_missing_from_run_also_halts(tmp_path):
    # edge: the OTHER torn branch -- a propagation-edge row with to_run + artifact but
    # NO from_run is equally undeterminable and must halt (the fix must not special-case
    # only the missing-to_run shape the bug report used).
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="propagation-edge", status="ok", subject="->c",
                   witness={"to_run": "c", "artifact": "peers/run/p"})     # NO from_run
    sr = FakeSlotRunner(slots=["s0"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and "malformed" in rep.reason
    assert rep.started == [] and sr.started == []


def test_preexisting_malformed_marker_still_halts(tmp_path):
    # edge (layer-2 independence): a durable malformed-edge MARKER from a prior tick --
    # with NO raw torn row left to re-parse -- must STILL halt. Guards that the layered
    # fix preserves the original marker-based safeguard, not just the new direct scan.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl._led.append(event="malformed-edge", status="halt",
                   subject="unparseable-propagation-edge", witness={"at": 0, "raw": None})
    sr = FakeSlotRunner(slots=["s0"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and rep.started == [] and sr.started == []


def test_well_formed_edge_does_not_halt_and_schedules(tmp_path):
    # happy (no false-positive halt): a fleet-ledger carrying a VALID, well-formed
    # propagation-edge must NOT trip the malformed-edge halt -- the conductor still
    # schedules the root run. Guards the fix against over-halting (which would brick
    # any fleet that has recorded a real consumption edge).
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(x)
    t = _commit_on_branch(x, "peers/run/p", "p.py", "x", peer="claude")
    fl.record_propagation_edge("p", "a", "peers/run/p", repo=x, tip_sha=t)  # WELL-FORMED
    sr = FakeSlotRunner(slots=["s0"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is False
    assert "a" in rep.started and ("s0", "a") in sr.started


def test_slot_mismatch_halts(tmp_path):
    # major F4-slot-mismatch: a runs on s1 but the ledger assigned it s0 -> halt.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0", "s1"], world={"s1": "a"})    # a on the WRONG slot
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and "slot" in rep.reason


def test_double_occupancy_halts(tmp_path):
    # major F4-double-start: the SAME run observed on two slots -> halt.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0", "s1"], world={"s0": "a", "s1": "a"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert rep.halted is True and ("double" in rep.reason or "two slots" in rep.reason)


def test_wedged_run_is_restarted(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "a"}, liveness={"a": "wedged"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted)
    assert "a" in rep.restarted and ("s0", "a") in sr.started


def test_conductor_writes_converged_itself_no_preseeded_row(tmp_path):
    # blocker F4-status: a 'done' run whose per-run ledger is_converged -> the conductor
    # (single writer) writes 'converged' ITSELF (no manual fleet record_status), frees the
    # slot, and routes. The masking pre-seed is DELETED here.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    tip = _commit_on_branch(x, "peers/run/a", "fix.py", "fix", peer="claude")
    a = _run(x, mode_run="a", tool=x, branch="peers/run/a")
    a._ledger = _converged_branch_ledger(x, x / "a.jsonl", "a", tip)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "a"}, liveness={"a": "done"})
    # the conductor re-verifies via a runs_by_id binding (its per-run ledger) -> converged
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr, ceiling=Ceiling(),
                       dep_check=lambda p, c: True, is_self_hosting=_trusted,
                       runs_by_id={"a": a}, repos_by_id={"a": x},
                       changed_paths_of=lambda rid: ["README.md"])   # docs-only -> trusted
    assert "a" in rep.converged
    assert fl.latest_status("a") == "converged"          # WRITTEN by the conductor
    assert "a" in rep.tier1                               # trusted landing routed Tier-1


def test_self_hosting_converged_run_routes_tier2_never_lands(tmp_path):
    # the REAL converged diff touches the spine -> Tier-2, never auto-lands.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    tip = _commit_on_branch(x, "peers/run/a", "fix.py", "fix", peer="claude")
    a = _run(x, mode_run="a", tool=x, branch="peers/run/a")
    a._ledger = _converged_branch_ledger(x, x / "a.jsonl", "a", tip)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "a"}, liveness={"a": "done"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr, ceiling=Ceiling(),
                       dep_check=lambda p, c: True, runs_by_id={"a": a}, repos_by_id={"a": x},
                       is_self_hosting=_self_hosting,            # spine-touch -> self-hosting
                       changed_paths_of=lambda rid: ["src/peers/spine/gates.py"])
    assert "a" in rep.tier2 and "a" not in rep.tier1
    assert fl.latest_status("a") != "landed"


def _converged_routing_fixture(tmp_path):
    # a single CONVERGED, auto-merge run ready for the routing step.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    tip = _commit_on_branch(x, "peers/run/a", "fix.py", "fix", peer="claude")
    a = _run(x, mode_run="a", tool=x, branch="peers/run/a")
    a._ledger = _converged_branch_ledger(x, x / "a.jsonl", "a", tip)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0"], world={"s0": "a"}, liveness={"a": "done"})
    return x, a, prog, fl, sr


def test_conductor_routes_from_the_REAL_converged_diff_not_changed_paths_none(tmp_path):
    # major F6-routing / M11 (test-honesty regression): the conductor MUST feed
    # is_self_hosting the REAL converged diff (changed_paths_of(run)), NEVER
    # changed_paths=None (which Stage-6 maps to self-hosting for EVERY run -> the Tier-1
    # batched-landing surface goes 100% DEAD). A CONSTANT is_self_hosting stub that ignores
    # changed_paths (as the other routing tests inject) cannot observe this -- the
    # `changed_paths=None` regression survives it. This spy DECIDES from the diff it
    # actually receives AND records it, so the regression fails here both ways.
    x, a, prog, fl, sr = _converged_routing_fixture(tmp_path)
    seen = {}

    def spy(repo, *, changed_paths, target_repo=None):
        seen["changed_paths"] = changed_paths           # capture exactly what the conductor passed
        # behave like the real detector: None/empty diff is undeterminable -> self-hosting;
        # a spine-touch is self-hosting; a docs-only diff is trusted.
        if not changed_paths:
            return (True, "undeterminable-diff")
        return (any("spine" in p for p in changed_paths), "computed-from-diff")

    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr, ceiling=Ceiling(),
                       dep_check=lambda p, c: True, runs_by_id={"a": a}, repos_by_id={"a": x},
                       is_self_hosting=spy,
                       changed_paths_of=lambda rid: ["docs/README.md"])   # docs-only -> trusted
    # (1) the conductor passed the REAL diff, not None -- kills `changed_paths=None`.
    assert seen["changed_paths"] == ["docs/README.md"]
    # (2) the routing decision FLOWED FROM that real diff: docs-only -> trusted -> Tier-1.
    #     Under the None regression the spy would see None -> self-hosting -> Tier-2, so
    #     this assertion ALSO fails (defense in depth on the same regression).
    assert "a" in rep.tier1 and "a" not in rep.tier2


def test_conductor_routes_a_real_spine_diff_to_tier2(tmp_path):
    # happy/sad symmetry via the SAME diff-deciding spy: a REAL spine-touching converged
    # diff must route Tier-2 (never auto-land), driven by the diff value, not a constant stub.
    x, a, prog, fl, sr = _converged_routing_fixture(tmp_path)
    seen = {}

    def spy(repo, *, changed_paths, target_repo=None):
        seen["changed_paths"] = changed_paths
        if not changed_paths:
            return (True, "undeterminable-diff")
        return (any("spine" in p for p in changed_paths), "computed-from-diff")

    rep = conduct_tick(fl, prog, Pool(slots=["s0"]), slot_runner=sr, ceiling=Ceiling(),
                       dep_check=lambda p, c: True, runs_by_id={"a": a}, repos_by_id={"a": x},
                       is_self_hosting=spy,
                       changed_paths_of=lambda rid: ["src/peers/spine/gates.py"])
    assert seen["changed_paths"] == ["src/peers/spine/gates.py"]
    assert "a" in rep.tier2 and "a" not in rep.tier1


def test_ceiling_block_surfaces_tier1(tmp_path):
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x, max_tokens=80), _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(tmp_path)
    fl.record_start_intent("a", "s0")
    fl.record_status("a", "running", slot="s0")
    sr = FakeSlotRunner(slots=["s0", "s1"], world={"s0": "a"})
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(max_tokens=150), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted, projected={"a": 80, "b": 100})
    assert any("ceiling" in str(t) for t in rep.tier1)
    assert "b" not in rep.started


def test_reject_run_marks_and_pulls_transitive_dependents(tmp_path):
    # major F3-reject: reject A -> B,C revoked, marked rejected, pulled from Tier-1.
    x = _repo(tmp_path, "x")
    prog = _program(_spec("a", tool=x), _spec("b", tool=x, depends_on=["a"]),
                    _spec("c", tool=x, depends_on=["b"]))
    fl = _fleet_ledger(tmp_path)
    _attested_repo(x)
    for frm, to in [("a", "b"), ("b", "c")]:
        t = _commit_on_branch(x, f"peers/run/{frm}-{to}", f"{frm}.py", "x", peer="claude")
        fl.record_propagation_edge(frm, to, f"peers/run/{frm}", repo=x, tip_sha=t)
    fl.record_status("b", "converged")
    fl.record_status("c", "converged")
    rep = reject_run(fl, "a", "gate-weakening", program=prog, tier1_stack=["b", "c"])
    assert fl.latest_status("b") == "rejected" and fl.latest_status("c") == "rejected"
    assert "b" not in rep.tier1 and "c" not in rep.tier1    # pulled from the landing stack


def test_reject_run_escalates_landed_dependent_to_tier2(tmp_path):
    # major F3-reject: a dependent that ALREADY landed cannot be silently un-landed ->
    # a rejected-after-land is an honesty stop -> Tier-2.
    x = _repo(tmp_path, "x")
    _attested_repo(x)
    prog = _program(_spec("a", tool=x), _spec("b", tool=x, depends_on=["a"]))
    fl = _fleet_ledger(tmp_path)
    t = _commit_on_branch(x, "peers/run/a-b", "a.py", "x", peer="claude")
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=x, tip_sha=t)
    fl.record_status("b", "landed")                        # b ALREADY landed
    rep = reject_run(fl, "a", "rejected-producer", program=prog, tier1_stack=[])
    assert "b" in rep.tier2                                 # landed-then-rejected -> Tier-2


def test_reject_run_escalates_landed_ROOT_to_tier2(tmp_path):
    # P0-b (review finding): the REJECTED run ITSELF, if already LANDED, must
    # escalate to Tier-2 and be pulled from the Tier-1 stack — symmetric with a
    # landed dependent. The original reject_run marked the root 'rejected' WITHOUT
    # capturing its prior status, silently un-landing it with no honesty stop.
    x = _repo(tmp_path, "x")
    _attested_repo(x)
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(tmp_path)
    fl.record_status("a", "landed")                        # the root itself ALREADY landed
    rep = reject_run(fl, "a", "rejected-after-land", program=prog, tier1_stack=["a"])
    assert fl.latest_status("a") == "rejected"
    assert "a" in rep.tier2                                 # landed-then-rejected ROOT -> Tier-2
    assert "a" not in rep.tier1                             # pulled from the landing stack


def test_intra_non_git_sha_producer_consumer_is_started_not_failed_closed(tmp_path):
    # P0-a (review finding): an intra-tool producer with a NON-git-sha converged
    # artifact (research file / find-bugs finding) has _converged_commit==None. The
    # consumer (same tool) is scheduled (dep_satisfied's intra branch needs only
    # is_converged, no git-sha), so the conductor must record a NON-ATTESTED intra
    # cascade edge and START it — NOT fail it closed to Tier-2 on an unrecordable edge.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    prog = _program(_spec("res", tool=x, mode="research"),
                    _spec("dev", tool=x, mode="develop", depends_on=["res"]))
    fl = _fleet_ledger(x)
    res = _run(x, mode_run="res", tool=x)                  # converged-but-no-git-sha (empty ledger)
    fl.record_status("res", "converged")
    sr = FakeSlotRunner(slots=["s0", "s1"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted,
                       runs_by_id={"res": res}, repos_by_id={"res": x, "dev": x})
    assert "dev" in rep.started                            # consumer STARTED
    assert not any("unrecordable" in str(t) for t in rep.tier2)   # NOT fail-closed
    assert ("res", "dev") in {(f, t) for f, t, _a in fl.propagation_edges()}
    assert cascade_invalidate(fl, "res") == {"dev"}        # cascade graph is complete


def test_intra_classification_survives_stage5_worktree_isolation(tmp_path):
    # P0-a (the MASKED case the skeptic flagged): under Stage-5 isolation each run
    # has its OWN leased worktree, so repos_by_id maps the two runs to DIFFERENT
    # paths even though they are the SAME tool. The intra/cross decision MUST use
    # the spec TOOL identity (stable), NOT repos_by_id — else the intra consumer is
    # mis-classified cross, requires a git-sha it doesn't have, and is re-stranded
    # to Tier-2. This test fails if _producer_edges keys intra/cross off repos_by_id.
    x = tmp_path / "x"
    x.mkdir()
    _attested_repo(x)
    wt_res = tmp_path / "wt-res"
    wt_res.mkdir()
    _attested_repo(wt_res)
    wt_dev = tmp_path / "wt-dev"
    wt_dev.mkdir()
    _attested_repo(wt_dev)
    prog = _program(_spec("res", tool=x, mode="research"),
                    _spec("dev", tool=x, mode="develop", depends_on=["res"]))
    fl = _fleet_ledger(x)
    res = _run(wt_res, mode_run="res", tool=wt_res)        # ran in its leased worktree, no git-sha
    fl.record_status("res", "converged")
    sr = FakeSlotRunner(slots=["s0", "s1"])
    rep = conduct_tick(fl, prog, Pool(slots=["s0", "s1"]), slot_runner=sr,
                       ceiling=Ceiling(), dep_check=lambda p, c: True,
                       is_self_hosting=_trusted, runs_by_id={"res": res},
                       repos_by_id={"res": wt_res, "dev": wt_dev})   # DISTINCT worktrees
    assert "dev" in rep.started
    assert not any("unrecordable" in str(t) for t in rep.tier2)
    assert ("res", "dev") in {(f, t) for f, t, _a in fl.propagation_edges()}


def test_revoked_dependent_is_reschedulable_after_reconverge(tmp_path):
    # major F4-reschedule: reject A -> B rejected; A re-converges (corrected); B is reset
    # so the scheduler can start it again against the corrected artifact.
    x = _repo(tmp_path, "x")
    _attested_repo(x)
    prog = _program(_spec("a", tool=x), _spec("b", tool=x, depends_on=["a"]))
    fl = _fleet_ledger(tmp_path)
    t = _commit_on_branch(x, "peers/run/a-b", "a.py", "x", peer="claude")
    fl.record_propagation_edge("a", "b", "peers/run/a", repo=x, tip_sha=t)
    fl.record_status("b", "converged")
    reject_run(fl, "a", "reverify-failed", program=prog, tier1_stack=[])
    assert fl.latest_status("b") == "rejected"
    # the conductor supersedes the stale edge + resets b to pending on a repair-reconverge
    fl.supersede_edge("a", "b")
    fl.record_status("b", "pending")
    from peers.fleet.scheduler import startable_runs
    res = startable_runs(prog, fl, Pool(slots=["s0", "s1"]), Ceiling(),
                         dep_check=lambda p, c: True)
    assert "b" in res.startable                            # reschedulable again
