"""STEP-5 — ``interpret()`` + an end-to-end ``drive()`` over a fake pipeline.

Driving a ``DevelopFrontend`` whose auditor yields one confirmable finding on
round 1 then nothing records: run-start → bar-inferred → gate → confirmed-work
(attested) → landing → dry rounds → stop; the spine gates all pass on the
resulting ledger. A run that only ever produces unsound findings terminates on
stop-on-dry with NO confirmed-work. A REAL but UNATTESTED converged commit does
NOT green the loop — the anti-fake-confirm guard holds at develop's own
``append_attested`` path.
"""
from __future__ import annotations

from peers.develop.frontend import DevelopFrontend
from peers.develop.ports import AuthoredContract, ImplementResult
from peers.spine.gates import all_pass, evaluate_spine_gates
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig
from peers.spine.stop_on_dry import dry_streak

from tests.unit._develop_helpers import (_F, _FixedAuditor, _FixedAuthor,
                                          _FixedImpl, _attested_repo,
                                          _develop_fe_all_unsound,
                                          _develop_fe_that_confirms_once,
                                          _repo_with_commit, _run)


# ---- happy: drive confirms once, lands, then stops; gates all pass -------
def test_develop_drive_confirms_then_stops(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    fe = _develop_fe_that_confirms_once(tmp_path, sha)
    run = _run(tmp_path)
    out = drive(run, fe)
    rows = run.ledger.read()
    assert any(r.event == "confirmed-work" for r in rows)
    assert any(r.event == "landing" for r in rows)
    assert rows[-1].event == "stop"
    assert all_pass(evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)) is True
    assert out["confirmed"] >= 1


def test_interpret_summarises_confirmed_rounds_and_last_landing(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    fe = _develop_fe_that_confirms_once(tmp_path, sha)
    run = _run(tmp_path)
    out = drive(run, fe)
    # the round 1 confirm + the trailing dry rounds are all rounds; the landing
    # target is the last branch-PR.
    assert out["confirmed"] == 1
    assert out["landing"] == "feat/x"
    assert out["rounds"] >= 1
    # interpret() is a pure read -> calling it again is stable.
    assert fe.interpret(run) == out


# ---- sad: an all-unsound run terminates dry with no confirmed-work -------
def test_develop_all_unsound_stops_dry_with_no_confirm(tmp_path):
    fe = _develop_fe_all_unsound(tmp_path)
    run = _run(tmp_path)
    drive(run, fe)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "stop" and rows[-1].status == "dry"


# ---- sad: a REAL but UNATTESTED confirm does not green the loop ----------
def test_develop_unattested_confirm_does_not_green_the_loop(tmp_path):
    # A real converged commit with NO peers-attest note must not count: the
    # confirmed-work row resolves author=None, so it does NOT reset the dry
    # streak and the authorship gate is False. Locks the anti-fake-confirm
    # guard at develop's own append_attested path.
    sha = _repo_with_commit(tmp_path)                 # real commit, UNATTESTED
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")  # present bar
    fe = DevelopFrontend(
        _FixedAuditor([_F("F1")]),                    # confirmable every round
        _FixedAuthor(AuthoredContract(plan_md="# p", acceptance="pytest -q",
                                      findings=["F1"])),
        _FixedImpl(ImplementResult(ok=True, head_sha=sha, branch="feat/x")),
        dimensions=["correctness"], run_tests=lambda c: (0, "1 passed"),
        refuter_factory=lambda f: (lambda i: False))
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "develop"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    drive(run, fe)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].author is None               # unattested -> no author
    # HS-05 (defense-in-depth lock): the row is written with independence=True
    # UNCONDITIONALLY -- NOT derived from whether the author resolved. That literal
    # is LOAD-BEARING: the authorship-attested gate only evaluates independence=True
    # rows (it `continue`s past the rest), so an independence=False here would make
    # the gate SKIP this unattested confirm and return True vacuously, silently
    # dropping a tested defense layer. Pin it so a future "harden to derived" cannot
    # erode the gate's independent rejection (the dry_streak layer below is the 2nd).
    assert cw[-1].independence is True
    assert dry_streak(rows) >= run.op_config.dry_n     # fake confirm did NOT reset
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    assert evaluate_spine_gates(rows, mode_run="r1",
                                repo=tmp_path)["authorship-attested"] is False
