from peers.research.ports import (Source, Witness, Claim, ReportArtifact,
    DecomposeResult, SweepResult, CompletenessVerdict, CommitResult,
    Decomposer, Sweeper, Synthesizer, Committer, CompletenessCritic,
    RefuterFactory)


def test_source_carries_the_cache_schema():
    s = Source(url="https://a.example/x", resolved_origin="a.example",
               content_hash="deadbeef", retrieval_time="2026-06-10T00:00:00Z",
               access_failure=None)
    assert s.resolved_origin == "a.example" and s.access_failure is None


def test_witness_links_a_source_or_a_code_location():
    w1 = Witness(kind="fetched-source", uri="https://a.example/x",
                 content_hash="deadbeef", resolved_origin="a.example")
    w2 = Witness(kind="code-location", uri="src/x.py:10",
                 content_hash="feedface", resolved_origin="repo:src/x.py")
    assert w1.kind == "fetched-source" and w2.uri == "src/x.py:10"


def test_claim_and_result_dataclasses():
    c = Claim(id="C1", text="asparagus roots from cuttings", status="unverified-gap",
              witnesses=[], load_bearing=True)
    assert c.id == "C1" and c.load_bearing is True
    assert ReportArtifact(path="/r/RESEARCH.md", content_hash="ab",
                          confirmed_ids=["C1"]).confirmed_ids == ["C1"]
    assert DecomposeResult(sub_questions=["q1"]).sub_questions == ["q1"]
    assert SweepResult(sources=[], code_locations=[]).sources == []
    assert CompletenessVerdict(state="work-done", not_checked=[]).state == "work-done"
    assert CommitResult(ok=True, head_sha="abc", branch="research/x").ok is True


def test_list_defaults_are_independent_instances():
    # field(default_factory=list) — NOT a shared mutable default.
    a = DecomposeResult()
    b = DecomposeResult()
    a.sub_questions.append("q")
    assert b.sub_questions == []
    c1 = Claim(id="C1", text="t", status="")
    c2 = Claim(id="C2", text="t", status="")
    c1.witnesses.append(
        Witness(kind="fetched-source", uri="u", content_hash="h", resolved_origin="o"))
    assert c2.witnesses == []


def test_protocols_are_runtime_checkable():
    # All FIVE capability ports are runtime-checkable Protocols (there is NO Verifier).
    class _D:
        def decompose(self, topic, repo):
            return DecomposeResult(sub_questions=[])

    class _S:
        def sweep(self, sub_question, repo, modalities):
            return SweepResult(sources=[], code_locations=[])

    class _Syn:
        def synthesize(self, claims, gaps, repo):
            return None

    class _Com:
        def implement(self, report, repo):
            return CommitResult(ok=True)

    class _Crit:
        def assess(self, claims, gaps, modalities_run, modalities_enabled):
            return CompletenessVerdict(state="finder-exhausted", not_checked=[])

    class _NotASweeper:   # missing sweep()
        def decompose(self, topic, repo):
            return DecomposeResult(sub_questions=[])

    assert isinstance(_D(), Decomposer) and isinstance(_S(), Sweeper)
    assert isinstance(_Syn(), Synthesizer) and isinstance(_Com(), Committer)
    assert isinstance(_Crit(), CompletenessCritic)
    assert not isinstance(_NotASweeper(), Sweeper)        # negative case


def test_refuter_factory_is_a_callable_alias_not_a_protocol():
    # The verify seam is a typed callable, NOT a port object.
    def f(claim):
        return lambda i: False

    rf: RefuterFactory = f
    assert callable(rf) and rf(None)(0) is False
