"""Stage-7 fleet DAEMON — the real process-spawning SlotRunner (TDD).

``ProcessSlotRunner`` is the boundary the conductor injects (``observe`` /
``liveness`` / ``start``). It manages one OS subprocess per slot (each child
leases a worktree + ``drive()``s one run), reads the spine-runs registry to
reconstruct each run's ModeRun for ``conduct_tick``, and reaps finished slots
two-phase so the conductor always sees a finished run ONCE before its slot frees.

Tests inject a fake ``launch`` returning a controllable child (no LLM): the
process lifecycle (spawn / observe / live / wedged / done / reap / restart) is
what we prove here; convergence re-verification is the conductor's job (its own
suite). happy / sad / edge each.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from peers.fleet.scheduler import Pool
from peers.fleet.slot_runner import ProcessSlotRunner
from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig
from tests.unit._fleet_helpers import _spec
from tests.unit._isolation_helpers import _git, _init_repo


def _repo(tmp_path, name):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    (p / "seed.py").write_text("x")
    _git(p, "add", "seed.py")
    _git(p, "commit", "-q", "-m", "seed")
    return p


def _sleep_child(seconds=60):
    """A child that just sleeps (no registry/ledger activity) — for live/wedged."""
    def launch(spec, base_sha):
        return subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            start_new_session=True)
    return launch


def _registry_child(repo, *, exit_after=True):
    """A child that writes a spine-runs registry record (+ an empty leased
    worktree ledger) like a real lease would, then exits (or sleeps)."""
    def launch(spec, base_sha):
        wt = repo / ".peers" / "wt" / spec.run_id
        script = textwrap.dedent(f"""
            import json, os
            from pathlib import Path
            repo = Path({str(repo)!r}); mr = {spec.run_id!r}
            wt = Path({str(wt)!r}); (wt / ".peers").mkdir(parents=True, exist_ok=True)
            ledger = wt / ".peers" / "run.jsonl"; ledger.write_text("")
            reg = repo / ".peers" / "spine-runs"; reg.mkdir(parents=True, exist_ok=True)
            (reg / (mr + ".json")).write_text(json.dumps({{
                "mode_run": mr, "worktree_path": str(wt),
                "branch": "peers/run/" + mr, "ledger_path": str(ledger),
                "pid": os.getpid(), "started_at": "now"}}))
            {"" if exit_after else "import time; time.sleep(60)"}
        """)
        return subprocess.Popen([sys.executable, "-c", script],
                                start_new_session=True)
    return launch


@pytest.fixture
def runner_factory(tmp_path):
    made = []

    def make(repos_by_id, **kw):
        pool = kw.pop("pool", Pool(slots=["s0", "s1"]))
        r = ProcessSlotRunner(pool, repos_by_id, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        r.shutdown()


# ---- happy ---------------------------------------------------------------
def test_start_then_observe_reports_run_on_slot(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x}, launch=_sleep_child())
    r.start("s0", _spec("a", tool=x))
    assert r.observe() == {"s0": "a", "s1": None}
    assert r.liveness("a") == "live"


def test_liveness_done_after_exit_then_reaped_next_observe(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x}, launch=_registry_child(x, exit_after=True))
    r.start("s0", _spec("a", tool=x))
    r._proc_of("a").wait(timeout=10)               # child has exited
    assert r.observe() == {"s0": "a", "s1": None}  # still reported (conductor sees it once)
    assert r.liveness("a") == "done"
    assert r.observe() == {"s0": None, "s1": None}  # now reaped -> slot free


def test_run_for_reconstructs_moderun_from_registry(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    head = _git(x, "rev-parse", "HEAD").strip()
    r = runner_factory({"a": x}, launch=_registry_child(x, exit_after=True))
    r.start("s0", _spec("a", tool=x))
    r._proc_of("a").wait(timeout=10)
    run = r.run_for("a")
    assert isinstance(run, ModeRun)
    assert run.mode_run == "a"
    assert run.base_sha == head                     # parent-chosen fork point
    assert run.branch == "peers/run/a"
    assert run.tool == x / ".peers" / "wt" / "a"
    assert run.ledger_path == run.tool / ".peers" / "run.jsonl"


def test_runs_by_id_collects_live_runs(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x, "b": x},
                       launch=_registry_child(x, exit_after=False))
    r.start("s0", _spec("a", tool=x))
    r.start("s1", _spec("b", tool=x))
    # give the children a moment to write their registry records
    for _ in range(50):
        if (x / ".peers" / "spine-runs" / "a.json").exists() and (
                x / ".peers" / "spine-runs" / "b.json").exists():
            break
        __import__("time").sleep(0.05)
    rbi = r.runs_by_id()
    assert set(rbi) == {"a", "b"}
    assert all(isinstance(v, ModeRun) for v in rbi.values())


# ---- sad / edge ----------------------------------------------------------
def test_liveness_wedged_when_idle_past_timeout(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    clock = {"t": 1000.0}
    r = runner_factory({"a": x}, launch=_sleep_child(),
                       now=lambda: clock["t"], idle_timeout_s=10)
    r.start("s0", _spec("a", tool=x))              # started_at = 1000 (fake clock)
    clock["t"] = 1005.0
    assert r.liveness("a") == "live"               # within idle window
    clock["t"] = 1011.0
    assert r.liveness("a") == "wedged"             # idle past timeout, proc alive


def test_restart_on_occupied_slot_kills_old_proc(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x}, launch=_sleep_child())
    r.start("s0", _spec("a", tool=x))
    old = r._proc_of("a")
    r.start("s0", _spec("a", tool=x))              # restart (wedged path)
    old.wait(timeout=10)
    assert old.poll() is not None                  # old proc was killed
    assert r._proc_of("a") is not old              # tracking the new proc
    assert r.observe() == {"s0": "a", "s1": None}


def test_liveness_unknown_run_is_done(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x}, launch=_sleep_child())
    assert r.liveness("ghost") == "done"           # never started -> treated as gone


def test_run_for_unknown_run_is_none(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x}, launch=_sleep_child())
    assert r.run_for("ghost") is None


def test_shutdown_kills_all_live_children(tmp_path, runner_factory):
    x = _repo(tmp_path, "x")
    r = runner_factory({"a": x, "b": x}, launch=_sleep_child())
    r.start("s0", _spec("a", tool=x))
    r.start("s1", _spec("b", tool=x))
    pa, pb = r._proc_of("a"), r._proc_of("b")
    r.shutdown()
    assert pa.poll() is not None and pb.poll() is not None


def test_default_launch_builds_run_one_argv(tmp_path):
    # the default (production) launch must invoke `python -m peers.fleet.run_one`
    # with a serialisable spec + the parent-chosen base — the deterministic part
    # of the spawn path (the actual Popen is exercised live).
    from peers.fleet.slot_runner import build_run_one_argv
    x = _repo(tmp_path, "x")
    spec = _spec("a", tool=x, mode="develop")
    argv = build_run_one_argv(spec, "deadbeef" * 5)
    assert argv[:4] == [sys.executable, "-m", "peers.fleet.run_one", "--spec"]
    payload = json.loads(argv[4])
    assert payload["run_id"] == "a" and payload["mode"] == "develop"
    assert payload["tool"] == str(x) and payload["base_sha"] == "deadbeef" * 5
    assert payload["op_config"]["mode"] == "develop"
    OpConfig.from_dict(payload["op_config"])        # round-trips into a real OpConfig
