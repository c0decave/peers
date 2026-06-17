from tests.unit._research_helpers import (_FixedDecomposer, _FixedSweeper, _AlwaysSynth,
                                          _FixedCritic, _FixedCommitter, _src, _run,
                                          _attested_repo)

from peers.research.frontend import ResearchFrontend
from peers.research.ports import SweepResult, CompletenessVerdict, CommitResult
from peers.spine.mode_run import ModeRun, drive
from peers.spine.op_config import OpConfig


def _confirming_fe(tmp_path, sha):
    return ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example"),
                     _src("https://b.example/y", "b.example")], code_locations=[]), once=True),
        synthesizer=_AlwaysSynth(tmp_path),
        committer=_FixedCommitter(CommitResult(ok=True, head_sha=sha, branch="research/x")),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[]),
                            CompletenessVerdict(state="finder-exhausted", not_checked=[])),
        modalities=["web"], run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False), k=2)


def test_research_propagation_emitted_when_branch_set(tmp_path):
    import hashlib
    from pathlib import Path
    sha = _attested_repo(tmp_path, "claude")
    run = ModeRun(tool=tmp_path, op_config=OpConfig.from_dict({"mode": "research"}),
                  ledger_path=tmp_path / "run.jsonl", mode_run="r1",
                  branch="peers/run/r1")
    _run(tmp_path)                                       # seed TOPIC.md into the same dir
    drive(run, _confirming_fe(tmp_path, sha))
    rows = run.ledger.read()
    prop = [r for r in rows if r.event == "propagation"]
    # research propagates a FILE witness (its native confirmed-work shape), NOT a
    # git-sha: the report file re-hashes from disk so the witness re-derives. The
    # row's author is the SUBSTRATE-attested peer of the committed head_sha (the
    # row is written via append_attested(repo, head_sha, ...)), never re-attested.
    assert prop and prop[-1].witness["kind"] == "file"
    assert prop[-1].author == "claude" and prop[-1].independence is True
    w = prop[-1].witness
    assert hashlib.sha256(Path(w["uri"]).read_bytes()).hexdigest() == w["sha256"]
