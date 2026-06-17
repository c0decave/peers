"""STEP-4 — run() adversarial VERIFY → classify by origin-independent witnesses.

Each load-bearing claim built by the SWEEP loop (STEP-3) is run through the
spine ``verify_claim`` k-vote gate via ``self.refuter_factory(claim)``; a claim
the refuters kill is dropped (no ``claim`` row, a ``gate`` fail row remains). A
SURVIVING claim is classified by the claim ledger over its origin-independent
witnesses and recorded as a ``claim`` ledger row carrying its status and the
independent-origin count. Synthesis/commit are STEP-5, so the round still ends
``dry-round`` here — nothing is confirmed-work yet.

The first four tests are the canonical contract bodies
(docs/plans/2026-06-10-agentic-os-stage-2.md, Task 4). The trailing two extend
3-class coverage (edge: a sub-question that swept nothing; sad: every source an
access failure) and hunt the adjacent BUG-528 path through verify+classify.
"""
from tests.unit._research_helpers import (_FixedDecomposer, _FixedSweeper, _NullSynth,
                                          _FixedCritic, _src, _run)

from peers.research.frontend import ResearchFrontend
from peers.research.ports import SweepResult, CompletenessVerdict


def _fe_with_sources(sources, **kw):
    base = dict(decomposer=_FixedDecomposer(["q1"]),
                sweeper=_FixedSweeper(SweepResult(sources=sources, code_locations=[])),
                synthesizer=_NullSynth(), committer=None,
                critic=_FixedCritic(CompletenessVerdict(state="finder-exhausted", not_checked=[])),
                modalities=["web"], run_tests=lambda cmd: None,
                refuter_factory=lambda c: (lambda i: False), k=3)   # survives
    base.update(kw)
    return ResearchFrontend(**base)


def test_unsound_claim_rejected_and_ledgered(tmp_path):
    run = _run(tmp_path)
    fe = _fe_with_sources([_src("https://a.example/x", "a.example"),
                           _src("https://b.example/y", "b.example")],
                          refuter_factory=lambda c: (lambda i: True), k=3)   # killed
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert any(r.event == "gate" and r.status == "fail" for r in rows)
    assert rows[-1].event == "dry-round"               # nothing confirmed
    # the killed claim is DROPPED — no `claim` row was written for it
    assert not [r for r in rows if r.event == "claim"]


def test_two_same_origin_witnesses_is_single_source_not_confirmed(tmp_path):
    run = _run(tmp_path)
    # two URLs, SAME resolved origin -> one independent witness -> single-source
    fe = _fe_with_sources([_src("https://a.example/x", "a.example"),
                           _src("https://a.example/z", "a.example")])
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    claim_rows = [r for r in rows if r.event == "claim"]
    assert claim_rows and claim_rows[-1].witness["status"] == "single-source"
    assert claim_rows[-1].witness["independent_origins"] == 1
    assert rows[-1].event == "dry-round"               # single-source is NOT a confirmed unit


def test_two_distinct_origins_confirmed(tmp_path):
    run = _run(tmp_path)
    fe = _fe_with_sources([_src("https://a.example/x", "a.example"),
                           _src("https://b.example/y", "b.example")])
    fe.prepare(run)
    fe.run(run)
    claim_rows = [r for r in run.ledger.read() if r.event == "claim"]
    assert claim_rows[-1].witness["status"] == "confirmed"
    assert claim_rows[-1].witness["independent_origins"] == 2


def test_one_claim_per_subquestion_cross_claim_origins_do_not_confirm(tmp_path):
    # TWO sub-questions, each given ONE distinct-origin source. The rule is ONE claim
    # per sub-question, so each claim has exactly ONE witness -> BOTH single-source.
    # If the projection were "all sources confirm any claim", these would wrongly confirm.
    run = _run(tmp_path)
    class _PerQSweeper:
        def sweep(self, sub_question, repo, modalities):
            origin = "a.example" if sub_question == "q1" else "b.example"
            return SweepResult(sources=[_src(f"https://{origin}/p", origin)], code_locations=[])
    fe = _fe_with_sources([], decomposer=_FixedDecomposer(["q1", "q2"]), sweeper=_PerQSweeper())
    fe.prepare(run)
    fe.run(run)
    claim_rows = [r for r in run.ledger.read() if r.event == "claim"]
    assert len(claim_rows) == 2
    assert all(r.witness["status"] == "single-source" for r in claim_rows)
    assert all(r.witness["independent_origins"] == 1 for r in claim_rows)


def test_claim_with_empty_witnesses_is_unverified_gap(tmp_path):
    # EDGE: a sub-question whose sweep returned ZERO usable evidence survives
    # verify (the refuter does not refute) but classifies as unverified-gap with
    # zero independent origins — it is routed to gaps, never confirmed, and the
    # round stays dry. A real caller hits this whenever a sub-question is unswept.
    run = _run(tmp_path)
    fe = _fe_with_sources([])                          # no sources, no code locations
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    claim_rows = [r for r in rows if r.event == "claim"]
    assert len(claim_rows) == 1
    assert claim_rows[-1].witness["status"] == "unverified-gap"
    assert claim_rows[-1].witness["independent_origins"] == 0
    assert rows[-1].event == "dry-round"


def test_every_source_access_failed_yields_no_confirmation(tmp_path):
    # SAD: two distinct-origin sources that BOTH failed to fetch. Per BUG-528 a
    # failed fetch yields no usable witness, so the surviving claim has zero
    # witnesses -> unverified-gap, NOT confirmed (a failed sweep must never green
    # a round just because the failures resolved distinct origins).
    run = _run(tmp_path)
    fe = _fe_with_sources([_src("https://a.example/x", "a.example", failure="dns"),
                           _src("https://b.example/y", "b.example", failure="timeout")])
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    claim_rows = [r for r in rows if r.event == "claim"]
    assert len(claim_rows) == 1
    assert claim_rows[-1].witness["status"] == "unverified-gap"
    assert claim_rows[-1].witness["independent_origins"] == 0
    assert rows[-1].event == "dry-round"
