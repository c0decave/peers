"""Stage-7 fleet DAEMON — the ``peers-ctl fleet`` CLI (TDD).

``peers-ctl fleet --manifest PATH`` loads + validates a fleet manifest, builds the
real SlotRunner + an ``auto_merge`` lander, runs ``conduct_fleet``, prints an
honest summary, and returns a meaningful exit code (0 only on a clean complete).
The daemon loop + SlotRunner have their own suites; here we prove the CLI wiring,
the fail-closed manifest path, and the exit-code contract — deterministic via an
injected conduct + slot-runner (no real spawning). happy / sad / edge each.
"""
import os

import yaml

from peers.fleet.daemon import FleetResult
from peers_ctl.cli import build_parser, cmd_fleet
from tests.unit._isolation_helpers import _init_repo


def _repo(tmp_path, name="tool"):
    p = tmp_path / name
    p.mkdir()
    _init_repo(p)
    return p


def _manifest_file(tmp_path, **over):
    repo = over.pop("repo", None) or _repo(tmp_path)
    raw = {
        "pool": {"slots": ["s0", "s1"]},
        "ceiling": {"max_runs": 2},
        "daemon": {"max_ticks": 20, "tick_sleep_s": 1, "target_ref": "main"},
        "runs": [
            {"run_id": "fbX", "tool": str(repo), "mode": "find-bugs:reproduce"},
            {"run_id": "devX", "tool": str(repo), "mode": "develop",
             "depends_on": ["fbX"], "landing": "auto-merge"},
        ],
    }
    raw.update(over)
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path, repo


class _FakeRunner:
    def __init__(self, *a, **k):
        self.shut = False

    def shutdown(self):
        self.shut = True


# ---- happy ---------------------------------------------------------------
def test_clean_complete_returns_zero(tmp_path):
    mpath, repo = _manifest_file(tmp_path)
    seen = {}

    def fake_conduct(fl, program, pool, ceiling, **kw):
        seen["runs"] = [s.run_id for s in program.runs]
        seen["slots"] = pool.slots
        seen["max_ticks"] = kw.get("max_ticks")
        return FleetResult(cause="complete", ticks=3,
                           statuses={"fbX": "converged", "devX": "landed"},
                           landed=["devX"], ok=True)

    runner = _FakeRunner()
    rc = cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl",
                   _conduct=fake_conduct, _make_slot_runner=lambda m: runner)
    assert rc == 0
    assert seen["runs"] == ["fbX", "devX"] and seen["slots"] == ["s0", "s1"]
    assert seen["max_ticks"] == 20                       # from the manifest
    assert runner.shut is True                           # slot runner torn down


def test_dry_run_validates_without_running(tmp_path):
    mpath, _ = _manifest_file(tmp_path)
    called = {"n": 0}

    def fake_conduct(*a, **k):
        called["n"] += 1
        return FleetResult(cause="complete", ticks=0, statuses={}, ok=True)

    rc = cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", dry_run=True,
                   _conduct=fake_conduct, _make_slot_runner=lambda m: _FakeRunner())
    assert rc == 0 and called["n"] == 0                  # never entered the loop


def test_defaults_in_tree_fleet_builders_for_the_run_one_subprocess(tmp_path, monkeypatch):
    # FLEET-02/SPEC-03: the registry is empty by default; the fleet CLI defaults
    # PEERS_FLEET_BUILDERS to the in-tree module so a real run can execute a mode.
    monkeypatch.delenv("PEERS_FLEET_BUILDERS", raising=False)
    mpath, _ = _manifest_file(tmp_path)
    cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", dry_run=True,
              _conduct=lambda *a, **k: FleetResult(cause="complete", ticks=0,
                                                   statuses={}, ok=True),
              _make_slot_runner=lambda m: _FakeRunner())
    assert os.environ.get("PEERS_FLEET_BUILDERS") == "peers.fleet.builders"


def test_does_not_override_an_operator_supplied_builders_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PEERS_FLEET_BUILDERS", "my.custom.builders")
    mpath, _ = _manifest_file(tmp_path)
    cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", dry_run=True,
              _conduct=lambda *a, **k: FleetResult(cause="complete", ticks=0,
                                                   statuses={}, ok=True),
              _make_slot_runner=lambda m: _FakeRunner())
    assert os.environ.get("PEERS_FLEET_BUILDERS") == "my.custom.builders"


# ---- sad -----------------------------------------------------------------
def test_incomplete_run_returns_nonzero(tmp_path):
    mpath, _ = _manifest_file(tmp_path)
    rc = cmd_fleet(
        str(mpath), ledger=tmp_path / "fleet.jsonl",
        _conduct=lambda *a, **k: FleetResult(
            cause="stalled", ticks=2, statuses={"fbX": "failed"}, ok=False),
        _make_slot_runner=lambda m: _FakeRunner())
    assert rc != 0


def test_missing_manifest_file_returns_two(tmp_path):
    rc = cmd_fleet(str(tmp_path / "nope.yaml"), ledger=tmp_path / "fleet.jsonl",
                   _conduct=lambda *a, **k: None,
                   _make_slot_runner=lambda m: _FakeRunner())
    assert rc == 2


def test_invalid_manifest_returns_two(tmp_path):
    repo = _repo(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(
        {"pool": {"slots": ["s0"]},
         "runs": [{"run_id": "a", "tool": str(repo), "mode": "develop",
                   "depends_on": ["ghost"]}]}))      # unknown dep -> validate fails
    rc = cmd_fleet(str(bad), ledger=tmp_path / "fleet.jsonl",
                   _conduct=lambda *a, **k: None,
                   _make_slot_runner=lambda m: _FakeRunner())
    assert rc == 2


def test_slot_runner_torn_down_even_when_conduct_raises(tmp_path):
    mpath, _ = _manifest_file(tmp_path)
    runner = _FakeRunner()

    def boom(*a, **k):
        raise RuntimeError("conduct blew up")

    rc = cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl",
                   _conduct=boom, _make_slot_runner=lambda m: runner)
    assert rc != 0 and runner.shut is True              # finally cleaned up


# ---- edge ----------------------------------------------------------------
def test_once_forces_single_tick(tmp_path):
    mpath, _ = _manifest_file(tmp_path)
    seen = {}

    def fake_conduct(*a, **k):
        seen["max_ticks"] = k.get("max_ticks")
        return FleetResult(cause="max-ticks", ticks=1, statuses={}, ok=False)

    cmd_fleet(str(mpath), ledger=tmp_path / "fleet.jsonl", once=True,
              _conduct=fake_conduct, _make_slot_runner=lambda m: _FakeRunner())
    assert seen["max_ticks"] == 1


def test_cli_parser_registers_fleet_subcommand():
    parser = build_parser()
    args = parser.parse_args(["fleet", "--manifest", "/tmp/x.yaml"])
    assert args.cmd == "fleet" and args.manifest == "/tmp/x.yaml"
