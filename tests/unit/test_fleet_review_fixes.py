"""Fixes for the fleet-daemon adversarial review (TDD: RED first, then fix).

Each test pins one confirmed finding from the 8-confirmed/6-refuted review:
zombie-process-leak, clock-mixing-hazard, git-head-failure-fatal (slot_runner);
FLEET-1/5 affinity validation (manifest); FLEET-4 builder rollback, FLEET-6/FLEET-2
spec validation, persistence fail-closed-read (run_one).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from peers.fleet import run_one
from peers.fleet.manifest import load_fleet_manifest
from peers.fleet.scheduler import Pool
from peers.fleet.slot_runner import ProcessSlotRunner, _terminate
from tests.unit._fleet_helpers import _spec
from tests.unit._isolation_helpers import _init_repo


def _repo(tmp_path, name="x"):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    (p / "s.py").write_text("x")
    _git_commit(p)
    return p


def _git_commit(p):
    subprocess.run(["git", "-C", str(p), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "c"], check=True,
                   capture_output=True)


# ---- zombie-process-leak (high) ------------------------------------------
class _FakeProc:
    """Stays alive through the first ``alive_for`` wait() calls (each raising
    TimeoutExpired), then is reaped. os.getpgid(this pid) raises -> _terminate
    falls back to terminate()/kill()."""

    def __init__(self, alive_for):
        self.pid = 2 ** 30          # almost certainly not a live pgid
        self._waits = 0
        self._alive_for = alive_for
        self._rc = None

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._waits += 1
        if self._waits > self._alive_for:
            self._rc = -9
            return self._rc
        raise subprocess.TimeoutExpired("child", timeout)

    def terminate(self):
        pass

    def kill(self):
        pass


def test_terminate_reaps_even_after_both_signal_waits_time_out():
    # SIGTERM wait times out, SIGKILL wait times out, but a final reap still runs.
    proc = _FakeProc(alive_for=2)        # survives the 2 per-signal waits
    _terminate(proc)
    assert proc.poll() is not None       # was reaped (no zombie)


# ---- clock-mixing-hazard (medium) ----------------------------------------
def test_liveness_ignores_future_mtime_under_injected_clock(tmp_path):
    x = _repo(tmp_path)
    clock = {"t": 1000.0}

    def launch(spec, base_sha):
        return subprocess.Popen(["sleep", "60"], start_new_session=True)

    r = ProcessSlotRunner(Pool(slots=["s0"]), {"a": x}, idle_timeout_s=10,
                          now=lambda: clock["t"], launch=launch)
    try:
        r.start("s0", _spec("a", tool=x))            # started_at = 1000 (fake)
        # a real-mtime activity file (far in the FUTURE vs the fake clock):
        reg = x / ".peers" / "spine-runs"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "a.json").write_text("{}")            # real mtime ~1.7e9
        clock["t"] = 1011.0                          # idle past timeout in fake domain
        assert r.liveness("a") == "wedged"           # future mtime ignored, not 'live'
    finally:
        r.shutdown()


# ---- git-head-failure-fatal (medium) -------------------------------------
def test_start_raises_clear_error_on_unresolvable_base(tmp_path):
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    r = ProcessSlotRunner(Pool(slots=["s0"]), {"a": notgit},
                          launch=lambda s, b: None)
    with pytest.raises(RuntimeError, match="cannot resolve base"):
        r.start("s0", _spec("a", tool=notgit))


# ---- FLEET-1 / FLEET-5 affinity validation (high) ------------------------
def test_manifest_rejects_run_affinity_not_in_pool(tmp_path):
    x = _repo(tmp_path)
    with pytest.raises(ValueError, match="affinity"):
        load_fleet_manifest({
            "pool": {"slots": ["s0"], "affinity": {"bigmem": "s0"}},
            "runs": [{"run_id": "a", "tool": str(x), "mode": "develop",
                      "affinity": "gpu"}]})       # 'gpu' not declared in pool


def test_manifest_accepts_run_affinity_in_pool(tmp_path):
    x = _repo(tmp_path)
    m = load_fleet_manifest({
        "pool": {"slots": ["s0"], "affinity": {"gpu": "s0"}},
        "runs": [{"run_id": "a", "tool": str(x), "mode": "develop",
                  "affinity": "gpu"}]})
    assert m.program.runs[0].affinity == "gpu"


# ---- FLEET-4 builder rollback (medium) -----------------------------------
def test_load_env_builders_rolls_back_on_install_failure():
    run_one._FRONTEND_BUILDERS.pop("develop", None)
    try:
        run_one._load_env_builders("tests.unit._fleet_builder_bad")
        # the module registered at import then install() raised -> must roll back
        assert "develop" not in run_one._FRONTEND_BUILDERS
    finally:
        run_one._FRONTEND_BUILDERS.pop("develop", None)


# ---- FLEET-6 / FLEET-2 spec validation (low, defense-in-depth) ------------
def _good_spec(tmp_path, **over):
    x = _repo(tmp_path)
    d = {"run_id": "a", "tool": str(x), "mode": "develop",
         "op_config": {"mode": "develop"}, "base_sha": "0" * 40}
    d.update(over)
    return d


def test_parse_spec_rejects_non_sha_base(tmp_path):
    with pytest.raises(ValueError, match="base_sha"):
        run_one.parse_spec(json.dumps(_good_spec(tmp_path, base_sha="not-a-sha")))


def test_parse_spec_rejects_traversal_run_id(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        run_one.parse_spec(json.dumps(_good_spec(tmp_path, run_id="../evil")))


def test_parse_spec_rejects_missing_tool_dir(tmp_path):
    with pytest.raises(ValueError, match="tool"):
        run_one.parse_spec(json.dumps(
            _good_spec(tmp_path, tool=str(tmp_path / "nope"))))


def test_parse_spec_accepts_valid(tmp_path):
    spec = run_one.parse_spec(json.dumps(_good_spec(tmp_path)))
    assert spec["run_id"] == "a"


# ---- persistence: corrupt persisted record is fail-closed (read None) -----
def test_read_fleet_run_treats_corrupt_record_as_absent(tmp_path):
    x = _repo(tmp_path)
    r = ProcessSlotRunner(Pool(slots=["s0"]), {"a": x}, launch=lambda s, b: None)
    stable = x / ".peers" / "fleet-runs" / "a"
    stable.mkdir(parents=True)
    (stable / "record.json").write_text("{ this is not json")
    assert r._read_fleet_run("a") is None        # corrupt -> absent (no partial trust)
