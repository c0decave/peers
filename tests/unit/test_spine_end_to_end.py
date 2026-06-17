"""STEP-9 — Stage-0 end-to-end acceptance (attested, real assertions).

These tests assemble the whole spine — `ModeRun` + `drive` + op-config + ledger +
substrate attestation + the fail-closed gate evaluator — and assert concrete
behaviours end to end (no `...` stubs that pass vacuously):

  - an attested confirmed-work + a re-derivable witness → ALL gates green;
  - a forged-author independence entry → authorship + witness gates reject it;
  - a no-progress run terminates via stop-on-dry;
  - the driven ledger is a tamper-evident hash chain;
  - the attested author actually flows onto the confirmed-work row.

No new production code beyond Tasks 1–8 / the drive loop.
"""
import hashlib
import subprocess

from peers import attest
from peers.spine.gates import all_pass, evaluate_spine_gates
from peers.spine.ledger import RunLedger
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig, load_op_config


def _git(p, *a):
    return subprocess.run(["git", "-C", str(p), *a], capture_output=True,
                          text=True, check=True).stdout


def _repo(p):
    _git(p, "init", "-q")
    _git(p, "config", "user.email", "t@t")
    _git(p, "config", "user.name", "t")
    _git(p, "config", "commit.gpgsign", "false")
    (p / "a.py").write_text("a")
    _git(p, "add", "a.py")
    _git(p, "commit", "-q", "-m", "a")
    base = _git(p, "rev-parse", "HEAD").strip()
    (p / "b.py").write_text("b")
    _git(p, "add", "b.py")
    _git(p, "commit", "-q", "-m", "b")
    sha = _git(p, "rev-parse", "HEAD").strip()
    return base, sha


class _GreenFE:
    """One attested confirmed-work round, then dry rounds."""

    def __init__(self, repo, sha, witness):
        self.repo, self.sha, self.witness, self.n = repo, sha, witness, 0

    def prepare(self, run):
        run.ledger.append(event="bar-inferred", status="pass")

    def run(self, run):
        self.n += 1
        if self.n == 1:
            run.ledger.append_attested(self.repo, self.sha, event="confirmed-work",
                                       subject="u1", status="pass",
                                       witness=self.witness, independence=True)
        else:
            run.ledger.append(event="dry-round", status="dry")

    def interpret(self, run):
        return {"ok": True, "rounds": self.n}


def _green_run(tmp_path):
    base, sha = _repo(tmp_path)
    attest.attest_commits(tmp_path, "claude", base, sha)
    ev = tmp_path / "evidence.txt"
    ev.write_text("green")
    wit = {"kind": "file", "uri": str(ev),
           "sha256": hashlib.sha256(b"green").hexdigest()}
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    out = drive(run, _GreenFE(tmp_path, sha, wit))
    return run, sha, out


def test_end_to_end_noop_run_passes_gates(tmp_path):
    run, _sha, _out = _green_run(tmp_path)
    res = evaluate_spine_gates(run.ledger.read(), mode_run="r1", repo=tmp_path)
    assert all_pass(res) is True


def test_end_to_end_attested_author_flows_to_confirmed_work(tmp_path):
    run, _sha, _out = _green_run(tmp_path)
    confirmed = [r for r in run.ledger.read() if r.event == "confirmed-work"]
    assert len(confirmed) == 1
    assert confirmed[0].author == "claude"          # substrate-attested, not caller
    assert confirmed[0].independence is True


def test_end_to_end_ledger_is_tamper_evident(tmp_path):
    run, _sha, _out = _green_run(tmp_path)
    p = run.ledger_path
    assert RunLedger(p).verify() is True
    # flip the attested author on disk -> chain digest no longer re-derives.
    p.write_text(p.read_text().replace('"author": "claude"', '"author": "mallory"'))
    assert RunLedger(p).verify() is False


def test_forged_author_entry_is_rejected(tmp_path):
    base, sha = _repo(tmp_path)   # NOT attested
    led = RunLedger(tmp_path / "run.jsonl")
    load_op_config(OpConfig.from_dict({"mode": "develop"}), led, mode_run="r1")
    led.append(event="confirmed-work", subject="u1", status="pass", author=None,
               witness={"kind": "file", "uri": str(tmp_path / "x"), "sha256": "x"},
               independence=True)
    res = evaluate_spine_gates(led.read(), mode_run="r1", repo=tmp_path)
    assert res["authorship-attested"] is False
    assert res["witness-ledgered"] is False
    assert all_pass(res) is False


def test_stop_on_dry_terminates(tmp_path):
    _repo(tmp_path)

    class DryFE:
        def prepare(self, run):
            run.ledger.append(event="bar-inferred", status="pass")

        def run(self, run):
            run.ledger.append(event="dry-round", status="dry")

        def interpret(self, run):
            return {}

    run = ModeRun(
        tool=tmp_path,
        op_config=OpConfig.from_dict({"mode": "develop", "budget": {"max_rounds": 12}}),
        ledger_path=tmp_path / "run.jsonl", mode_run="r1",
    )
    drive(run, DryFE())
    rows = run.ledger.read()
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
