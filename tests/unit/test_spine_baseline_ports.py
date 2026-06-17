from peers.spine.baseline import (CandidateBaseline, BaselineResult, BaselineAuthor,
                                  OUTCOME_BUILT, OUTCOME_REUSED, OUTCOME_UNCHARACTERIZABLE)
from peers.spine.direction import Bar

def test_candidate_baseline_shape():
    c = CandidateBaseline(path="/r/test_characterization.py",
                          command="python3 -m pytest test_characterization.py")
    assert c.path.endswith("test_characterization.py") and "pytest" in c.command

def test_baseline_result_shape_and_outcome_sentinels():
    r = BaselineResult(outcome=OUTCOME_BUILT, bar=Bar("present", "python3 -m pytest",
                                                      exit_code=0, output="1 passed"))
    assert r.outcome == OUTCOME_BUILT and r.bar.kind == "present"
    assert r.witness is None and r.artifact_path is None       # scalar defaults
    assert {OUTCOME_BUILT, OUTCOME_REUSED, OUTCOME_UNCHARACTERIZABLE} == \
        {"built", "reused", "uncharacterizable"}

def test_baseline_author_is_runtime_checkable_positive_and_negative():
    class _A:
        def author(self, repo, bar): return None
    class _NotAnAuthor:                       # missing author()
        def write(self, repo, bar): return None
    assert isinstance(_A(), BaselineAuthor)
    assert not isinstance(_NotAnAuthor(), BaselineAuthor)       # negative case
