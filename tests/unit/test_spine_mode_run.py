"""STEP-4 — ModeRun record + ModeFrontend protocol + the drive loop.

A `ModeRun` binds (tool, op-config, ledger path, mode_run id). A `ModeFrontend`
is a Protocol (`prepare`/`run`/`interpret`). `drive(run, frontend)` is a real
loop: it logs the op-config (`run-start`), prepares, runs rounds until stop-on-dry
fires (`stop(status='dry')`) or the budget cap is reached (`stop(status=
'complete')`), then returns `interpret`. The stop row is emitted on BOTH paths.

Covers happy (lifecycle: run-start → confirmed-work → … → stop(dry)), edge
(budget cap → stop(complete); prepare runs once before the loop; ledger is lazy),
sad (a frontend that never makes progress still terminates).
"""
from peers.spine.mode_run import ModeFrontend, ModeRun, drive
from peers.spine.op_config import OpConfig


class _Fake:
    # one round of (fake) work, then dry rounds -> drive should stop on dry
    def __init__(self):
        self.calls = 0

    def prepare(self, run):
        run.ledger.append(event="bar-inferred", status="pass")

    def run(self, run):
        self.calls += 1
        if self.calls == 1:
            run.ledger.append(event="confirmed-work", subject="u1", status="pass",
                              witness={"kind": "exit-code", "uri": "pytest",
                                       "sha256": "deadbeef"})
        else:
            run.ledger.append(event="dry-round", status="dry")

    def interpret(self, run):
        return {"rounds": self.calls}


def test_drive_records_lifecycle_and_stops(tmp_path):
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    out = drive(run, _Fake())
    events = [r.event for r in run.ledger.read()]
    assert events[0] == "run-start" and "confirmed-work" in events and events[-1] == "stop"
    assert run.ledger.read()[-1].status in ("dry", "complete")
    assert out["rounds"] >= 1


def test_fake_satisfies_protocol():
    # the Protocol is runtime-checkable so frontends can be validated.
    assert isinstance(_Fake(), ModeFrontend)


def test_run_start_row_carries_op_config(tmp_path):
    # edge: drive logs the op-config as the FIRST row (witness kind op-config).
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "research"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="rX")
    drive(run, _Fake())
    rows = run.ledger.read()
    assert rows[0].event == "run-start"
    assert rows[0].witness["kind"] == "op-config" and rows[0].mode_run == "rX"
    # prepare's bar-inferred row comes after run-start, before any round
    assert rows[1].event == "bar-inferred"


class _NoOp:
    # never appends a round row -> the dry streak never grows -> only the
    # budget cap can stop it.
    def prepare(self, run):
        run.ledger.append(event="bar-inferred", status="pass")

    def run(self, run):
        pass

    def interpret(self, run):
        return {"done": True}


def test_budget_cap_stops_with_complete(tmp_path):
    # edge: a frontend that makes neither dry nor confirmed rounds hits the
    # max_rounds cap and stops with status 'complete'.
    run = ModeRun(
        tool=tmp_path,
        op_config=OpConfig.from_dict({"mode": "develop", "budget": {"max_rounds": 3}}),
        ledger_path=tmp_path / "run.jsonl", mode_run="r1",
    )
    out = drive(run, _NoOp())
    rows = run.ledger.read()
    assert rows[-1].event == "stop" and rows[-1].status == "complete"
    assert out == {"done": True}


class _AlwaysDry:
    def prepare(self, run):
        run.ledger.append(event="bar-inferred", status="pass")

    def run(self, run):
        run.ledger.append(event="dry-round", status="dry")

    def interpret(self, run):
        return {}


def test_always_dry_terminates_on_dry(tmp_path):
    # sad: no progress at all still terminates (stop-on-dry), never loops forever.
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    drive(run, _AlwaysDry())
    rows = run.ledger.read()
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    assert run.ledger.verify() is True       # the whole driven ledger stays intact


def test_drive_fails_closed_on_torn_ledger(tmp_path):
    # A torn trailing line (a crash mid-append) must NOT crash drive(); it must
    # still terminate the ledger with an explicit stop row (fail-closed), mirroring
    # verify()'s posture. read()/verify() stay strict; only the driver recovers.
    import json
    p = tmp_path / "run.jsonl"

    class _TornFE:
        def prepare(self, run):
            pass

        def run(self, run):
            run.ledger.append(event="dry-round", status="dry")
            with p.open("a", encoding="utf-8") as fh:
                fh.write("{partial-torn-line")   # interrupted write, no newline

        def interpret(self, run):
            return {"ok": True}

    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=p, mode_run="r1")
    out = drive(run, _TornFE())                  # must not raise
    assert out["ok"] is True
    last = [ln for ln in p.read_text().splitlines() if ln.strip()][-1]
    assert json.loads(last)["event"] == "stop"
    assert json.loads(last)["status"] == "aborted"


def test_ledger_is_lazy_and_cached(tmp_path):
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    assert run.ledger is run.ledger          # same cached RunLedger instance
