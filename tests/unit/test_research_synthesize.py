"""STEP-5 — run() SYNTHESIZE → commit → confirmed-work, guarded by the
completeness critic. The first three tests are the contract's canonical
fail-first bodies (Task 5); the remainder exhaustively exercise the other
branches of the new synth/commit guard (happy / edge / sad).

Load-bearing facts under test:
- a confirmed-work unit is **substrate-attested** (author from the peers-attest
  note, never caller content) and **file-witnessed** (the report re-hashes from
  disk), so the spine ``witness-ledgered`` gate passes;
- the completeness critic is the stop-on-dry guard — a ``finder-exhausted`` round
  is dry even with a confirmed claim;
- the report ``file`` witness is only valid because the **Synthesizer is the SOLE
  writer**; a Committer that re-writes the file changes its hash → dry-round;
- a non-resolvable commit sha never greens the loop (``resolves_to_commit`` is
  the rejector, not the file-hash re-derive).
"""
from pathlib import Path

from tests.unit._research_helpers import (
    _AlwaysSynth,
    _FixedCommitter,
    _FixedCritic,
    _FixedDecomposer,
    _FixedSweeper,
    _NullSynth,
    _attested_repo,
    _run,
    _src,
)

from peers.research.frontend import ResearchFrontend
from peers.research.ports import CommitResult, CompletenessVerdict, SweepResult
from peers.spine.gates import evaluate_spine_gates


def _confirming_fe(tmp_path, sha, *, critic_state="work-done", commit_sha=None,
                   synthesizer=None, committer=None, sources=None):
    """A ResearchFrontend that corroborates one claim from TWO origin-independent
    sources (so STEP-4 classifies it ``confirmed``), then synthesizes + commits.
    The ``synthesizer`` / ``committer`` / ``sources`` hooks let a single test swap
    one collaborator to exercise a specific guard branch."""
    if sources is None:
        sources = [_src("https://a.example/x", "a.example"),
                   _src("https://b.example/y", "b.example")]
    return ResearchFrontend(
        decomposer=_FixedDecomposer(["q1"]),
        sweeper=_FixedSweeper(SweepResult(sources=sources, code_locations=[]),
                              once=True),
        synthesizer=synthesizer or _AlwaysSynth(tmp_path),
        committer=committer or _FixedCommitter(
            CommitResult(ok=True, head_sha=commit_sha or sha, branch="research/x")),
        critic=_FixedCritic(CompletenessVerdict(state=critic_state, not_checked=[])),
        modalities=["web"], run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False), k=2)


# ---- canonical contract bodies (Task 5) --------------------------------------
def test_confirmed_unit_is_attested_and_file_witnessed(tmp_path):
    sha = _attested_repo(tmp_path, "claude")           # real commit + peers-attest note
    fe = _confirming_fe(tmp_path, sha)
    run = _run(tmp_path)                               # also writes TOPIC.md into the same dir
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].author == "claude" and cw[-1].independence is True
    assert cw[-1].witness["kind"] == "file"            # the report artifact, re-hashed from disk
    assert evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)["witness-ledgered"] is True


def test_finder_exhausted_round_is_dry_even_with_a_confirmed_claim(tmp_path):
    # The COMPLETENESS CRITIC is the stop-on-dry guard: a skipped modality makes the
    # round finder-exhausted, so it must NOT advance the stop counter.
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(tmp_path, sha, critic_state="finder-exhausted")
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"


def test_non_resolvable_commit_is_not_a_gated_confirm(tmp_path):
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(tmp_path, sha, commit_sha="deadbeef")   # fake head_sha
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    # 'deadbeef' fails resolves_to_commit -> the frontend takes the dry-round branch,
    # so NO confirmed-work is appended and witness-ledgered is False for lack of one.
    assert not any(r.event == "confirmed-work" for r in rows)
    assert evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)["witness-ledgered"] is False


# ---- thorough branch coverage (every other path through the new guard) --------
def test_landing_row_records_branch_pr_after_confirmed_work(tmp_path):
    """HAPPY+: a confirmed unit also publishes a ``landing`` row naming the branch
    (a plain append — publication is not a substrate-attested authorship event)."""
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(tmp_path, sha)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    landing = [r for r in rows if r.event == "landing"]
    assert landing and landing[-1].subject == "research/x"
    assert landing[-1].witness["landing"] == "branch-pr"
    assert landing[-1].witness["kind"] == "url"
    # the landing row carries NO author (it is a plain append, not attested)
    assert landing[-1].author is None and landing[-1].independence is False
    # ordering: confirmed-work is recorded BEFORE its landing publication
    events = [r.event for r in rows]
    assert events.index("confirmed-work") < events.index("landing")


def test_synthesizer_returning_none_is_dry(tmp_path):
    """SAD: a confirmed claim + work-done critic but the Synthesizer declines to
    write a report (returns None) → dry-round, never confirmed-work."""
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(tmp_path, sha, synthesizer=_NullSynth())
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"


def test_committer_not_ok_is_dry(tmp_path):
    """SAD: the report is written but the commit FAILS (ok=False) → dry-round, no
    confirmed-work (a half-done commit must never green the loop)."""
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(
        tmp_path, sha,
        committer=_FixedCommitter(
            CommitResult(ok=False, head_sha=sha, branch="research/x", reason="rejected")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"
    # the synthesizer (sole writer) still wrote its report to disk before the
    # commit was attempted — the dry-round is the COMMIT failing, not the write.
    assert (tmp_path / "RESEARCH.md").exists()


def test_committer_rewriting_report_breaks_file_witness(tmp_path):
    """SAD / load-bearing: 'Synthesizer writes, Committer only commits'. A
    Committer that re-writes the report file changes its on-disk hash, so the
    ``file_sha == report.content_hash`` re-derive FAILS → dry-round, even though
    the commit itself is real and attested."""
    sha = _attested_repo(tmp_path, "claude")

    class _RewritingCommitter:
        """A malicious/buggy committer that normalises (mutates) the report it was
        handed, invalidating the synthesizer's reported hash."""

        def implement(self, report, repo):
            Path(report.path).write_text("# Report (rewritten by committer)\n")
            return CommitResult(ok=True, head_sha=sha, branch="research/x")

    fe = _confirming_fe(tmp_path, sha, committer=_RewritingCommitter())
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"
    # witness-ledgered is False for lack of any confirmed-work row
    assert evaluate_spine_gates(rows, mode_run="r1", repo=tmp_path)["witness-ledgered"] is False
    # the file on disk is the committer's rewrite (proves the mutation happened)
    assert "rewritten by committer" in (tmp_path / "RESEARCH.md").read_text()


def test_missing_committer_is_dry(tmp_path):
    """SAD: a confirming pipeline wired with NO committer cannot publish, so the
    round must fail CLOSED to a dry-round — never raise an AttributeError that
    ``drive()`` would let escape, and never green the loop."""
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(tmp_path, sha)
    fe.committer = None                                # caller forgot the committer
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"


def test_single_source_claim_does_not_confirm_despite_work_done_critic(tmp_path):
    """EDGE: a claim with only ONE origin-independent witness classifies as
    ``single-source`` → ``self._confirmed`` is empty → even a work-done critic
    yields a dry-round (the guard's ``self._confirmed`` half), and no report is
    synthesized."""
    sha = _attested_repo(tmp_path, "claude")
    fe = _confirming_fe(
        tmp_path, sha,
        sources=[_src("https://a.example/x", "a.example")])  # ONE origin only
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    claim_rows = [r for r in rows if r.event == "claim"]
    assert claim_rows and claim_rows[-1].witness["status"] == "single-source"
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"
    # nothing confirmed -> the synthesizer was never asked to write a report
    assert not (tmp_path / "RESEARCH.md").exists()
