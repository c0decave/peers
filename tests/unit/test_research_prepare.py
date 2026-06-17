"""STEP-2 — ResearchFrontend.prepare(): runs the intake, records the bar (without
ever blocking on an absent one — research is a KNOWLEDGE mode), and stashes the
brief body into ``self._topic_text`` so Task 3's decomposer never sees an unset
attribute."""
from tests.unit._research_helpers import (
    _FixedCritic,
    _NullDecomposer,
    _NullSweeper,
    _NullSynth,
    _topic,
)

from peers.research.frontend import ResearchFrontend
from peers.research.ports import CompletenessVerdict
from peers.spine.mode_run import ModeRun
from peers.spine.op_config import OpConfig


def _fe(**kw):
    base = dict(
        decomposer=_NullDecomposer(),
        sweeper=_NullSweeper(),
        synthesizer=_NullSynth(),
        committer=None,
        critic=_FixedCritic(
            CompletenessVerdict(state="finder-exhausted", not_checked=[])),
        modalities=["web", "codebase"],
        run_tests=lambda cmd: None,
    )
    base.update(kw)
    return ResearchFrontend(**base)


def _mk_run(tmp_path):
    return ModeRun(
        tool=tmp_path,
        op_config=OpConfig.from_dict({"mode": "research"}),
        ledger_path=tmp_path / "run.jsonl",
        mode_run="r1",
    )


def test_prepare_records_bar_and_topic_present_does_not_block(tmp_path):
    _topic(tmp_path)                                   # valid generic brief, NO repo/bar
    run = _mk_run(tmp_path)
    fe = _fe()
    fe.prepare(run)
    assert fe._blocked is False                        # no bar, but research still runs
    assert any(r.event == "bar-inferred" for r in run.ledger.read())


def test_prepare_stashes_topic_body(tmp_path):
    _topic(tmp_path)
    run = _mk_run(tmp_path)
    fe = _fe()
    fe.prepare(run)
    # the brief BODY reaches the decomposer via self._topic_text (a passing-fake
    # decomposer ignores its arg, so assert the body here so the read can't be masked)
    assert "Scope" in fe._topic_text
    assert "Questions" in fe._topic_text
    assert "asparagus" in fe._topic_text


def test_prepare_missing_topic_blocks_and_empties_topic_text(tmp_path):
    run = _mk_run(tmp_path)
    fe = _fe()
    fe.prepare(run)
    assert fe._blocked is True                          # no brief -> blocked (honest dry)
    assert fe._topic_text == ""                         # blocked -> empty, never unset


def test_prepare_present_bar_boundary_still_runs(tmp_path):
    # edge: a grounding repo WHOSE BAR IS PRESENT (a runner + a green result)
    # records a non-absent bar but must NOT gate the knowledge run — only a
    # missing brief blocks, never the bar kind.
    _topic(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    run = _mk_run(tmp_path)
    fe = _fe(run_tests=lambda cmd: (0, "ok"))
    fe.prepare(run)
    assert fe.bar.kind == "present"
    assert fe._blocked is False
    bar_rows = [r for r in run.ledger.read() if r.event == "bar-inferred"]
    assert bar_rows
    assert bar_rows[0].status == "pass"
