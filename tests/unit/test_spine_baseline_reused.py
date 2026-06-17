from tests.unit._baseline_landing_helpers import _FileAuthor, _green
from peers.spine.baseline import build_baseline, OUTCOME_REUSED, OUTCOME_BUILT
from peers.spine.direction import Bar
from peers.spine.ledger import RunLedger

def test_reused_delegates_to_snapshot_when_weak_bar_reruns_green(tmp_path):
    # A WEAK bar (red at detect, exit_code=1) whose runner RE-RUNS green via run_tests
    # (_green -> (0, "1 passed")): the snapshot already pins behavior -> 'reused' (no new
    # characterization file authored). The snapshot delegate is injected.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    ledger = RunLedger(tmp_path / "run.jsonl")
    seen = {}
    def _snapshot(repo):
        seen["called"] = True
        return "no_regression: captured baseline"   # the existing machinery's success msg
    res = build_baseline(tmp_path, _green, author=_FileAuthor(),
                         bar=Bar("weak", "python3 -m pytest", exit_code=1),
                         ledger=ledger, mode_run="r1", snapshot=_snapshot)
    assert res.outcome == OUTCOME_REUSED and res.bar.provenance == "reused"
    assert seen.get("called") is True
    assert not (tmp_path / "test_characterization.py").exists()   # author NOT invoked

def test_reused_unavailable_on_empty_snapshot_falls_through_to_built(tmp_path):
    # The runner re-runs green BUT the snapshot returns None (no green tests on disk to
    # snapshot — the empty-baseline case no_regression.py:176-185 just-passes on): the
    # 'reused' path is NOT taken; the builder AUTHORS observations instead -> 'built'.
    # This is the proof that the snapshot does NOT solve the weak/absent case.
    ledger = RunLedger(tmp_path / "run.jsonl")
    res = build_baseline(tmp_path, _green, author=_FileAuthor(),
                         bar=Bar("weak", "python3 -m pytest", exit_code=1),
                         ledger=ledger, mode_run="r1", snapshot=lambda repo: None)
    assert res.outcome == OUTCOME_BUILT and res.bar.provenance == "built"
    assert (tmp_path / "test_characterization.py").exists()        # authored
