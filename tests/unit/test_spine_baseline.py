from tests.unit._baseline_landing_helpers import (
    _hash, _FileAuthor, _NullAuthor, _green, _red, _norun)
from peers.spine.baseline import (build_baseline, ensure_bar,
                                  OUTCOME_BUILT, OUTCOME_UNCHARACTERIZABLE)
from peers.spine.direction import Bar, infer_bar
from peers.spine.ledger import RunLedger

def test_bar_has_detected_provenance_by_default(tmp_path):
    # infer_bar's Bars are 'detected' -> the existing detector path is unchanged.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    bar = infer_bar(tmp_path, _green)
    assert bar.kind == "present" and bar.provenance == "detected"

def test_build_baseline_upgrades_absent_to_built_on_green(tmp_path):
    # An ABSENT bar (no runner) + an author that writes a real test + a GREEN run -> built.
    ledger = RunLedger(tmp_path / "run.jsonl")
    res = build_baseline(tmp_path, _green, author=_FileAuthor(),
                         bar=Bar("absent", None), ledger=ledger, mode_run="r1")
    assert res.outcome == OUTCOME_BUILT
    assert res.bar.kind == "present" and res.bar.provenance == "built"
    # the written artifact is witnessed by a `file` witness whose sha256 re-hashes from disk:
    assert res.witness["kind"] == "file"
    assert res.witness["sha256"] == _hash(res.witness["uri"]) == _hash(res.artifact_path)

def test_build_baseline_green_only_red_run_is_uncharacterizable(tmp_path):
    # The author WROTE a file, but the authored tests are RED -> NOT upgraded (green-only).
    res = build_baseline(tmp_path, _red, author=_FileAuthor(), bar=Bar("absent", None))
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE and res.bar.kind == "absent"
    assert res.witness is None                 # a red baseline emits no file witness

def test_build_baseline_unre_hashable_artifact_is_uncharacterizable(tmp_path):  # kind: sad
    # Fail-closed (defense in depth): a GREEN run but the witnessed artifact cannot
    # be re-hashed (it vanished / was symlink-swapped / is non-regular between the
    # author-write and the re-hash) -> uncharacterizable, witness=None, and the
    # OSError NEVER escapes (drive() does not catch frontend exceptions, so an escape
    # would crash the whole run). Locks the baseline.py OSError branch — the
    # adversarial review proved a `raise` mutation there leaves the rest green.
    from peers.spine.baseline import CandidateBaseline

    written = tmp_path / "test_characterization.py"

    class _AuthorWritesAReal:
        def author(self, repo, bar):
            written.write_text("# pinned current behaviour\n")
            return CandidateBaseline(path=str(written), command="pytest -q")

    def _green_then_vanish(cmd):
        if written.exists():           # the artifact races away after a green run
            written.unlink()
        return (0, "1 passed")

    res = build_baseline(tmp_path, _green_then_vanish, author=_AuthorWritesAReal(),
                         bar=Bar("absent", None))
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE   # returned, did NOT raise
    assert res.witness is None
    assert res.bar.kind == "absent"                   # not upgraded — fail-closed

def test_build_baseline_norun_is_uncharacterizable(tmp_path):
    res = build_baseline(tmp_path, _norun, author=_FileAuthor(), bar=Bar("absent", None))
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE and res.bar.kind == "absent"

def test_build_baseline_no_author_candidate_is_uncharacterizable(tmp_path):
    # The author cannot even propose a characterization test -> honest stop.
    res = build_baseline(tmp_path, _green, author=_NullAuthor(), bar=Bar("absent", None))
    assert res.outcome == OUTCOME_UNCHARACTERIZABLE and res.bar.kind == "absent"

def test_ensure_bar_present_bar_skips_the_builder(tmp_path):  # kind: edge
    # A present bar needs no builder: ensure_bar returns the detected bar untouched.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    ledger = RunLedger(tmp_path / "run.jsonl")
    # author would RAISE if called -> proves the builder was NOT invoked on a present bar.
    class _Boom:
        def author(self, repo, bar): raise AssertionError("builder must not run on present")
    bar = ensure_bar(tmp_path, _green, author=_Boom(), ledger=ledger, mode_run="r1")
    assert bar.kind == "present" and bar.provenance == "detected"

def test_ensure_bar_builds_on_absent(tmp_path):
    ledger = RunLedger(tmp_path / "run.jsonl")
    bar = ensure_bar(tmp_path, _green, author=_FileAuthor(), ledger=ledger, mode_run="r1")
    assert bar.kind == "present" and bar.provenance == "built"
    rows = ledger.read()
    assert any(r.event == "bar-inferred" for r in rows)        # infer_bar's own row
    assert any(r.event == "baseline-built" for r in rows)      # the builder's row

def test_ensure_bar_uncharacterizable_stays_absent(tmp_path):
    ledger = RunLedger(tmp_path / "run.jsonl")
    bar = ensure_bar(tmp_path, _norun, author=_NullAuthor(), ledger=ledger, mode_run="r1")
    assert bar.kind == "absent" and bar.provenance == "detected"
    # the builder records its failure to characterize as a NON-confirmed row (not independence):
    rows = ledger.read()
    blt = [r for r in rows if r.event == "baseline-built"]
    assert all(r.independence is False for r in blt)
