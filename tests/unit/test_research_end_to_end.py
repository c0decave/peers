"""STEP-6 — ``interpret()`` + an end-to-end ``drive()`` over a fake pipeline.

These tests close the loop: ``drive()`` runs ``prepare`` → rounds → terminal
``stop`` and returns ``frontend.interpret(run)``. The fake ports (from
``_research_helpers``) make the orchestration deterministic — no live web / no
LLM. Four behaviours are locked:

  - a pipeline that confirms ONE corroborated claim in round 1 then sweeps
    nothing drives to a confirmed-work + landing unit and passes ALL FOUR spine
    gates, then stops on dry;
  - a pipeline that can never corroborate (zero sources, finder-exhausted critic)
    stops dry with NO confirmed-work;
  - the per-round-reset lock: a once=True sweeper + a work-done-EVERY-round critic
    still emits confirmed-work in round 1 ONLY (rounds 2+ have an empty
    self._confirmed → dry), so stop-on-dry still fires;
  - an UNATTESTED real commit does not green the loop (author resolves None → the
    streak does not reset and the authorship gate is False).
"""
from tests.unit._research_helpers import (_research_fe_that_confirms_once,
                                          _research_fe_finder_exhausted,
                                          _FixedDecomposer, _FixedSweeper, _AlwaysSynth,
                                          _FixedCritic, _FixedCommitter, _src, _run, _topic,
                                          _attested_repo, _repo_with_commit)

from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig
from peers.spine.gates import evaluate_spine_gates, all_pass
from peers.spine.stop_on_dry import dry_streak
from peers.research.frontend import ResearchFrontend
from peers.research.ports import SweepResult, CompletenessVerdict, CommitResult


def test_research_drive_confirms_then_stops(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    fe = _research_fe_that_confirms_once(tmp_path, sha)
    run = _run(tmp_path)
    out = drive(run, fe)
    rows = run.ledger.read()
    assert any(r.event == "confirmed-work" for r in rows)
    assert any(r.event == "landing" for r in rows)
    assert any(r.event == "claim" and r.witness["status"] == "confirmed" for r in rows)
    assert rows[-1].event == "stop"
    assert all_pass(evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)) is True
    assert out["confirmed"] >= 1


def test_research_finder_exhausted_stops_dry_with_no_confirm(tmp_path):
    fe = _research_fe_finder_exhausted(tmp_path)
    run = _run(tmp_path)
    drive(run, fe)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "stop" and rows[-1].status == "dry"


def test_research_workdone_critic_still_stops_dry(tmp_path):
    # The per-round-reset lock: sweeper once=True yields sources ONLY in round 1, but a
    # buggy/over-eager critic returns work-done EVERY round. Confirmed-work must appear in
    # round 1 ONLY (rounds 2+ have an empty self._confirmed -> dry-round), so the streak
    # advances and the loop stops on dry. (Without the per-round reset, round 2 would
    # re-confirm the stale round-1 claim and reset the streak forever.)
    sha = _attested_repo(tmp_path, "claude")
    fe = ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example"),
                     _src("https://b.example/y", "b.example")], code_locations=[]), once=True),
        synthesizer=_AlwaysSynth(tmp_path),
        committer=_FixedCommitter(CommitResult(ok=True, head_sha=sha, branch="research/x")),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[])),  # work-done EVERY round
        modalities=["web"], run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False), k=2)
    run = _run(tmp_path)
    drive(run, fe)
    rows = run.ledger.read()
    assert len([r for r in rows if r.event == "confirmed-work"]) == 1   # round 1 only
    assert rows[-1].event == "stop" and rows[-1].status == "dry"


def test_research_unattested_confirm_does_not_green_the_loop(tmp_path):
    # A REAL committed report with NO peers-attest note: author resolves None, so it
    # does NOT reset the dry streak and the authorship gate is False.
    sha = _repo_with_commit(tmp_path)                  # real commit, UNATTESTED
    _topic(tmp_path)
    fe = ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example"),
                     _src("https://b.example/y", "b.example")], code_locations=[])),  # confirmable EVERY round
        synthesizer=_AlwaysSynth(tmp_path),
        committer=_FixedCommitter(CommitResult(ok=True, head_sha=sha, branch="research/x")),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[])),
        modalities=["web"], run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False), k=2)
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "research"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1")
    drive(run, fe)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].author is None                # unattested -> no author
    assert dry_streak(rows) >= run.op_config.dry_n      # the fake confirm did NOT reset the streak
    assert rows[-1].event == "stop" and rows[-1].status == "dry"
    assert evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)["authorship-attested"] is False
