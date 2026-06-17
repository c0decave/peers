"""Stage-7 fleet DAEMON — the ``conduct_fleet`` tick loop (TDD).

The loop is the missing production caller of ``conduct_tick``: assemble run
objects from the SlotRunner, tick, land converged auto-merge runs, surface Tier-2,
drive the reject cascade, and TERMINATE HONESTLY (complete / halted / stalled /
ceiling / max-ticks / aborted). Deterministic: a fake SlotRunner + injected
lander / clock (no LLM, no real subprocess — the ProcessSlotRunner lifecycle is
its own suite). Mirrors the conductor/e2e fixtures. happy / sad / edge each.
"""
from peers.fleet.daemon import FleetResult, conduct_fleet
from peers.fleet.scheduler import Ceiling, Pool
from peers.spine.auto_merge import LandingResult
from peers.spine.ledger import RunLedger
from peers.spine.op_config import OpConfig, load_op_config
from tests.unit._fleet_helpers import FakeSlotRunner, _fleet_ledger, _program, _spec
from tests.unit._isolation_helpers import _attested_repo, _run
from tests.unit.test_fleet_conductor import _converged_run

NOSLEEP = lambda *_a, **_k: None          # noqa: E731 — test stub
TRUSTED = lambda r, **kw: (False, "")     # noqa: E731 — nothing self-hosting


def _repo(tmp_path, name="x"):
    p = tmp_path / name
    p.mkdir()
    _attested_repo(p)
    return p


def _nonconverged_run(repo, mode_run):
    r = _run(repo, mode_run=mode_run, tool=repo)
    led = RunLedger(repo / f"{mode_run}.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run=mode_run)
    led.append(event="stop", status="aborted", mode_run=mode_run)
    r._ledger = led
    return r


class _DaemonFake(FakeSlotRunner):
    """A FakeSlotRunner that also exposes ``runs_by_id`` (the daemon's extra
    contract). ``runs`` maps run_id -> ModeRun; ``liveness`` is scripted."""

    def __init__(self, *, slots, runs=None, world=None, liveness=None):
        super().__init__(slots=slots, world=world, liveness=liveness)
        self._runs = dict(runs or {})

    def runs_by_id(self):
        return dict(self._runs)


def _land_ok(calls):
    def lander(run, *, repo, target_ref):
        calls.append((run.mode_run, str(repo), target_ref))
        return LandingResult(landed=True, merged_sha="a" * 40, target_ref=target_ref)
    return lander


# ---- happy ---------------------------------------------------------------
def test_intra_dag_converges_and_auto_merge_run_lands(tmp_path):
    x = _repo(tmp_path)
    prog = _program(
        _spec("fbX", tool=x, mode="find-bugs:reproduce"),
        _spec("devX", tool=x, mode="develop", depends_on=["fbX"], landing="auto-merge"))
    fl = _fleet_ledger(x)
    runs = {"fbX": _converged_run(x, "fbX"), "devX": _converged_run(x, "devX")}
    sr = _DaemonFake(slots=["s0", "s1"], runs=runs,
                     liveness={"fbX": "done", "devX": "done"})
    calls = []
    # dep gating: devX may start only once fbX is recorded converged in the ledger.
    res = conduct_fleet(
        fl, prog, Pool(slots=["s0", "s1"]), Ceiling(),
        slot_runner=sr, repos_by_id={"fbX": x, "devX": x},
        dep_check=lambda p, c: fl.latest_status(p) == "converged",
        is_self_hosting=TRUSTED, lander=_land_ok(calls),
        max_ticks=10, sleep=NOSLEEP, target_ref="main")
    assert isinstance(res, FleetResult)
    assert res.cause == "complete" and res.ok is True
    assert fl.latest_status("fbX") == "converged"
    assert fl.latest_status("devX") == "landed"
    assert res.landed == ["devX"]
    assert calls == [("devX", str(x), "main")]          # the lander was driven once


def test_single_root_auto_merge_lands(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], runs={"a": _converged_run(x, "a")},
                     liveness={"a": "done"})
    res = conduct_fleet(fl, prog, Pool(slots=["s0"]), Ceiling(),
                        slot_runner=sr, repos_by_id={"a": x},
                        dep_check=lambda p, c: True, is_self_hosting=TRUSTED,
                        lander=_land_ok([]), max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "complete" and res.ok is True
    assert fl.latest_status("a") == "landed"


def test_converged_branch_pr_run_is_a_clean_terminal(tmp_path):
    # a branch-pr converged run is SUCCESS (the PR is intentionally manual); no land.
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x, landing="branch-pr"))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], runs={"a": _converged_run(x, "a")},
                     liveness={"a": "done"})
    res = conduct_fleet(fl, prog, Pool(slots=["s0"]), Ceiling(),
                        slot_runner=sr, repos_by_id={"a": x},
                        dep_check=lambda p, c: True, is_self_hosting=TRUSTED,
                        max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "complete" and res.ok is True
    assert fl.latest_status("a") == "converged" and res.landed == []


# ---- sad -----------------------------------------------------------------
def test_world_divergence_halts_the_fleet(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], world={"s0": "intruder"})    # unknown run on s0
    res = conduct_fleet(fl, prog, Pool(slots=["s0"]), Ceiling(),
                        slot_runner=sr, repos_by_id={"a": x},
                        dep_check=lambda p, c: True, is_self_hosting=TRUSTED,
                        max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "halted" and res.ok is False
    assert "divergence" in res.halt_reason


def test_failed_producer_strands_dependent_then_stalls(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("fbX", tool=x, mode="find-bugs:reproduce"),
                    _spec("devX", tool=x, mode="develop", depends_on=["fbX"]))
    fl = _fleet_ledger(x)
    runs = {"fbX": _nonconverged_run(x, "fbX"), "devX": _converged_run(x, "devX")}
    sr = _DaemonFake(slots=["s0", "s1"], runs=runs, liveness={"fbX": "done"})
    res = conduct_fleet(
        fl, prog, Pool(slots=["s0", "s1"]), Ceiling(),
        slot_runner=sr, repos_by_id={"fbX": x, "devX": x},
        dep_check=lambda p, c: fl.latest_status(p) == "converged",
        is_self_hosting=TRUSTED, max_ticks=10, sleep=NOSLEEP)
    assert res.cause == "stalled" and res.ok is False
    assert fl.latest_status("fbX") == "failed"
    assert fl.latest_status("devX") in (None, "pending")        # never started


def test_ceiling_blocked_fleet_stops_for_raise_or_stop(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x, max_tokens=80), _spec("b", tool=x, max_tokens=100))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0", "s1"])
    res = conduct_fleet(fl, prog, Pool(slots=["s0", "s1"]),
                        Ceiling(max_tokens=70),            # neither run fits
                        slot_runner=sr, repos_by_id={"a": x, "b": x},
                        dep_check=lambda p, c: True, is_self_hosting=TRUSTED,
                        projected={"a": 80, "b": 100}, max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "ceiling" and res.ok is False


def test_self_hosting_converged_run_surfaces_tier2_not_clean(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], runs={"a": _converged_run(x, "a")},
                     liveness={"a": "done"})
    res = conduct_fleet(
        fl, prog, Pool(slots=["s0"]), Ceiling(),
        slot_runner=sr, repos_by_id={"a": x}, dep_check=lambda p, c: True,
        is_self_hosting=lambda r, **kw: (True, "target-is-peers"),
        changed_paths_of=lambda rid: ["src/peers/spine/gates.py"],
        lander=_land_ok([]), max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "complete" and res.ok is False     # needs human review
    assert "a" in res.tier2
    assert fl.latest_status("a") == "converged"            # NEVER auto-landed (§6.3)


# ---- edge ----------------------------------------------------------------
def test_run_that_never_converges_hits_max_ticks(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], runs={"a": _converged_run(x, "a")},
                     liveness={"a": "live"})              # never finishes
    res = conduct_fleet(fl, prog, Pool(slots=["s0"]), Ceiling(),
                        slot_runner=sr, repos_by_id={"a": x},
                        dep_check=lambda p, c: True, is_self_hosting=TRUSTED,
                        max_ticks=3, sleep=NOSLEEP)
    assert res.cause == "max-ticks" and res.ok is False
    assert res.ticks == 3


def test_should_reject_cascades_through_the_loop(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("fbX", tool=x, mode="find-bugs:reproduce"),
                    _spec("devX", tool=x, mode="develop", depends_on=["fbX"]))
    fl = _fleet_ledger(x)
    runs = {"fbX": _converged_run(x, "fbX"), "devX": _converged_run(x, "devX")}
    sr = _DaemonFake(slots=["s0", "s1"], runs=runs,
                     liveness={"fbX": "done", "devX": "done"})
    # a post-convergence skeptic reopens fbX once its dependent devX has converged.
    def should_reject(run_id, ledger):
        if run_id == "fbX" and ledger.latest_status("devX") == "converged":
            return "skeptic-reopened"
        return None
    res = conduct_fleet(
        fl, prog, Pool(slots=["s0", "s1"]), Ceiling(),
        slot_runner=sr, repos_by_id={"fbX": x, "devX": x},
        dep_check=lambda p, c: fl.latest_status(p) == "converged",
        is_self_hosting=TRUSTED, should_reject=should_reject,
        max_ticks=10, sleep=NOSLEEP)
    assert res.cause == "complete" and res.ok is False
    assert fl.latest_status("fbX") == "rejected"
    assert fl.latest_status("devX") == "rejected"          # cascaded transitively


def test_unexpected_error_is_an_honest_aborted_terminal(tmp_path):
    x = _repo(tmp_path)
    prog = _program(_spec("a", tool=x, landing="auto-merge"))
    fl = _fleet_ledger(x)
    sr = _DaemonFake(slots=["s0"], runs={"a": _converged_run(x, "a")},
                     liveness={"a": "done"})

    def boom(repo, **kw):
        raise RuntimeError("self-hosting probe exploded")

    res = conduct_fleet(fl, prog, Pool(slots=["s0"]), Ceiling(),
                        slot_runner=sr, repos_by_id={"a": x},
                        dep_check=lambda p, c: True, is_self_hosting=boom,
                        max_ticks=5, sleep=NOSLEEP)
    assert res.cause == "aborted" and res.ok is False
    assert "exploded" in res.error
