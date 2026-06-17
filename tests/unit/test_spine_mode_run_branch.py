from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig


class _NoopFrontend:
    def prepare(self, run): pass
    def run(self, run):
        run.ledger.append(event="dry-round", status="dry", mode_run=run.mode_run)
    def interpret(self, run):
        return {"branch": run.branch}


def _mk(tmp_path, **kw):
    base = dict(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    base.update(kw)
    return ModeRun(**base)


def test_branch_defaults_none_legacy_construction(tmp_path):
    run = _mk(tmp_path)                          # no branch kwarg -> legacy
    assert run.branch is None


def test_branch_is_keyword_settable(tmp_path):
    run = _mk(tmp_path, branch="peers/run/r1")
    assert run.branch == "peers/run/r1"


def test_drive_unchanged_by_branch_presence(tmp_path):
    run = _mk(tmp_path, branch="peers/run/r1")
    out = drive(run, _NoopFrontend())
    rows = run.ledger.read()
    assert rows[0].event == "run-start"
    assert rows[-1].event == "stop"
    assert out["branch"] == "peers/run/r1"       # frontend can read run.branch
