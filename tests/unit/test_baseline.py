from peers.spine.baseline import (
    OUTCOME_BUILT,
    OUTCOME_UNCHARACTERIZABLE,
    CandidateBaseline,
    build_baseline,
    ensure_bar,
)
from peers.spine.direction import Bar
from peers.spine.ledger import RunLedger
from tests.unit._baseline_landing_helpers import _FileAuthor, _green, _red


def test_baseline_happy_builds_file_witness_from_author(tmp_path):
    ledger = RunLedger(tmp_path / "run.jsonl")
    res = build_baseline(
        tmp_path, _green, author=_FileAuthor(), bar=Bar("absent", None),
        ledger=ledger, mode_run="r1",
    )
    assert res.outcome == OUTCOME_BUILT
    assert res.witness["kind"] == "file"
    assert res.artifact_path == res.witness["uri"]


def test_baseline_edge_present_bar_skips_author(tmp_path):
    class _Boom:
        def author(self, repo, bar):
            raise AssertionError("builder must not run on a present bar")

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    bar = ensure_bar(tmp_path, _green, author=_Boom())
    assert bar.kind == "present"
    assert bar.provenance == "detected"


def test_baseline_sad_red_candidate_is_uncharacterizable(tmp_path):
    res = build_baseline(
        tmp_path, _red, author=_FileAuthor(), bar=Bar("absent", None),
    )
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE
    assert res.witness is None


def test_baseline_sad_vanished_artifact_is_uncharacterizable(tmp_path):
    written = tmp_path / "test_characterization.py"

    class _AuthorWritesAReal:
        def author(self, repo, bar):
            written.write_text("# pinned current behaviour\n")
            return CandidateBaseline(path=str(written), command="pytest -q")

    def _green_then_vanish(cmd):
        written.unlink()
        return (0, "1 passed")

    res = build_baseline(
        tmp_path, _green_then_vanish, author=_AuthorWritesAReal(),
        bar=Bar("absent", None),
    )
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE
    assert res.witness is None
