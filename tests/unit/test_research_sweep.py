from tests.unit._research_helpers import (
    _FixedCritic,
    _FixedDecomposer,
    _FixedSweeper,
    _NullSynth,
    _run,
    _src,
)

from peers.research.claim_ledger import (
    CONFIRMED,
    SINGLE_SOURCE,
    UNVERIFIED_GAP,
    classify_claim,
)
from peers.research.frontend import ResearchFrontend
from peers.research.ports import CompletenessVerdict, SweepResult


def _fe(**kw):
    base = dict(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(
            sources=[_src("https://a.example/x", "a.example")], code_locations=[])),
        synthesizer=_NullSynth(), committer=None,
        critic=_FixedCritic(CompletenessVerdict(state="finder-exhausted", not_checked=["codebase"])),
        modalities=["web"], run_tests=lambda cmd: None,
    )
    base.update(kw)
    return ResearchFrontend(**base)


def test_blocked_run_is_dry(tmp_path):
    run = _run(tmp_path, with_topic=False)             # no brief -> blocked
    fe = _fe()
    fe.prepare(run)
    fe.run(run)
    assert run.ledger.read()[-1].event == "dry-round"


def test_sweep_records_sources_to_cache_and_ledger(tmp_path):
    run = _run(tmp_path)
    fe = _fe()
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    sweep = [r for r in rows if r.event == "sweep"]
    assert sweep and sweep[-1].witness["sources"] == 1 and "web" in sweep[-1].witness["modalities"]
    # the fetched source is persisted to the run's source cache, keyed by content_hash
    from peers.research.source_cache import SourceCache
    cache = SourceCache(run.tool / "sources.jsonl")
    assert cache.by_content_hash(_src("https://a.example/x", "a.example").content_hash) is not None


def test_modalities_run_reflects_a_skipped_modality(tmp_path):
    # modalities_enabled = web + codebase, but the sweeper only services 'web'
    # (returns web-style sources only), so the sweep row's modalities_run must NOT
    # claim codebase ran — this is the signal a finder-exhausted critic acts on.
    run = _run(tmp_path)
    fe = _fe(modalities=["web", "codebase"],
             sweeper=_FixedSweeper(SweepResult(
                 sources=[_src("https://a.example/x", "a.example")],
                 code_locations=[])))           # web-style source only; codebase yields nothing
    fe.prepare(run)
    fe.run(run)
    sweep = [r for r in run.ledger.read() if r.event == "sweep"]
    assert "web" in sweep[-1].witness["modalities"]
    assert "codebase" not in sweep[-1].witness["modalities"]   # the gap is recorded, not aliased


def test_empty_decomposition_yields_no_sweep_rows(tmp_path):
    # edge: a decomposer that returns ZERO sub-questions sweeps nothing — no
    # sweep rows, no crash, and the round still ends dry.
    run = _run(tmp_path)
    fe = _fe(decomposer=_FixedDecomposer([]))
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "sweep" for r in rows)
    assert rows[-1].event == "dry-round"


def test_failed_only_source_is_cached_but_its_modality_not_run(tmp_path):
    # sad: a sweep whose ONLY source is an access failure still records the
    # source (failures are never silently dropped) but does NOT count 'web' as
    # run — an access-failure-only result is the finder-exhausted signal, not
    # coverage. The sweep row counts the source yet reports no modality.
    run = _run(tmp_path)
    fe = _fe(sweeper=_FixedSweeper(SweepResult(
        sources=[_src("https://down.example/x", "down.example", failure="timeout")],
        code_locations=[])))
    fe.prepare(run)
    fe.run(run)
    sweep = [r for r in run.ledger.read() if r.event == "sweep"]
    assert sweep[-1].witness["sources"] == 1            # the failed fetch is recorded
    assert sweep[-1].witness["modalities"] == []        # but it counts as no coverage
    from peers.research.source_cache import SourceCache
    cache = SourceCache(run.tool / "sources.jsonl")
    got = cache.by_content_hash(
        _src("https://down.example/x", "down.example", failure="timeout").content_hash)
    assert got is not None and got.access_failure == "timeout"


# ---- BUG-528 regression: a FAILED fetch must NEVER become a witness ----
# A failed fetch still carries a non-empty resolved_origin (e.g. a post-DNS
# timeout) + content_hash, but §5.2 says "a failed fetch yields no usable
# witness". run()'s witness projection must skip access-failed sources so the
# claim ledger cannot count them toward the >=2-origin confirmation threshold.
# classify_claim (STEP-1) is the oracle here; STEP-4 will drive the same path
# through verify+classify, but the projection bug lives in STEP-3 so it is
# pinned at this seam (the candidate claim stashed for STEP-4).
def _claim_after_run(tmp_path, sweep_result):
    run = _run(tmp_path)
    fe = _fe(sweeper=_FixedSweeper(sweep_result))
    fe.prepare(run)
    fe.run(run)
    assert len(fe._round_claims) == 1            # one claim per sub-question
    return fe, run, fe._round_claims[0]


def test_two_real_distinct_origins_confirm(tmp_path):
    # happy: two REAL origin-independent fetches -> two witnesses -> confirmed.
    # Proves the BUG-528 access_failure filter does NOT strip legitimate
    # witnesses (no regression on the confirm path).
    _fe_, _run_, claim = _claim_after_run(tmp_path, SweepResult(
        sources=[_src("https://a.example/x", "a.example"),
                 _src("https://b.example/y", "b.example")],
        code_locations=[]))
    assert len(claim.witnesses) == 2
    assert classify_claim(claim) == CONFIRMED


def test_real_plus_failed_distinct_origin_is_single_source_not_confirmed(tmp_path):
    # edge: one REAL source (origin A) + one FAILED source (origin B,
    # a post-DNS timeout that still resolved a non-empty origin). The failed
    # fetch must NOT corroborate: only the real source witnesses -> exactly one
    # independent origin -> single-source, never confirmed. BEFORE the fix this
    # classified CONFIRMED, greening the loop on a fetch that failed.
    # distinct bodies -> distinct content_hashes (the cache is content-addressed
    # first-wins, so a shared body would alias the two sources).
    _fe_, run, claim = _claim_after_run(tmp_path, SweepResult(
        sources=[_src("https://a.example/x", "a.example", "real-body"),
                 _src("https://down.example/y", "down.example", "fail-body",
                      failure="timeout")],
        code_locations=[]))
    assert [w.resolved_origin for w in claim.witnesses] == ["a.example"]
    assert classify_claim(claim) == SINGLE_SOURCE
    # defense in depth: the failed fetch is still RECORDED in the cache (failures
    # are never silently dropped) — it simply yields no witness.
    from peers.research.source_cache import SourceCache
    cache = SourceCache(run.tool / "sources.jsonl")
    failed = _src("https://down.example/y", "down.example", "fail-body",
                  failure="timeout")
    assert cache.by_content_hash(failed.content_hash).access_failure == "timeout"


def test_all_failed_sweep_yields_no_witness_gap(tmp_path):
    # sad: every source in the sweep is an access failure -> ZERO witnesses ->
    # unverified-gap (routed to the report gaps, never confirmed-work), even
    # though both failures carry distinct non-empty resolved origins.
    _fe_, _run_, claim = _claim_after_run(tmp_path, SweepResult(
        sources=[_src("https://a.example/x", "a.example", failure="dns"),
                 _src("https://b.example/y", "b.example", failure="timeout")],
        code_locations=[]))
    assert claim.witnesses == []
    assert classify_claim(claim) == UNVERIFIED_GAP
