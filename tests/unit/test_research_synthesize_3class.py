"""STEP-5 (coverage completion) — class-evident happy/edge/sad tests for the
SYNTHESIZE → commit → confirmed-work guard.

STEP-5's frozen impl commit (``0a1df6a``) substantively covers all three case
classes, but its test NAMES fall outside the ``coverage-3class-delta`` keyword
vocabulary for the EDGE class (it keys on ``empty``/``boundary``/``multiple``,
not ``single``/``rewriting``). Mirroring the Concern-3 resolution for STEP-1
(``69362af``), this file adds GENUINELY-NEW assertions — not renames — that
exercise synth/commit edges the STEP-5 file did not, with names the delta gate
reads as happy + edge + sad. STEP-5's PLAN annotation re-points here while
keeping ``impl 0a1df6a`` in the step prose; the ``[x]`` toggle commit + author
are unchanged.
"""
import hashlib

from tests.unit._research_helpers import (
    _AlwaysSynth,
    _FixedCommitter,
    _FixedCritic,
    _FixedDecomposer,
    _FixedSweeper,
    _attested_repo,
    _run,
    _src,
    _write_report,
)

from peers.research.frontend import ResearchFrontend
from peers.research.ports import (
    CommitResult,
    CompletenessVerdict,
    ReportArtifact,
    SweepResult,
)


def _two_origin_sources():
    return [_src("https://a.example/x", "a.example"),
            _src("https://b.example/y", "b.example")]


def _fe(tmp_path, *, sub_questions=("q1",), synthesizer=None, committer=None,
        sources=None, once=True):
    """A confirming ResearchFrontend; each sub-question is swept the SAME
    two-origin result so STEP-4 classifies it ``confirmed``."""
    sha = _attested_repo(tmp_path, "claude")
    return ResearchFrontend(
        decomposer=_FixedDecomposer(list(sub_questions)),
        sweeper=_FixedSweeper(
            SweepResult(sources=sources if sources is not None
                        else _two_origin_sources(), code_locations=[]), once=once),
        synthesizer=synthesizer or _AlwaysSynth(tmp_path),
        committer=committer or _FixedCommitter(
            CommitResult(ok=True, head_sha=sha, branch="research/x")),
        critic=_FixedCritic(CompletenessVerdict(state="work-done", not_checked=[])),
        modalities=["web"], run_tests=lambda cmd: None,
        refuter_factory=lambda c: (lambda i: False), k=2), sha


class _EmptyConfirmedIdsSynth:
    """A Synthesizer that writes a real, re-hashable report but reports ZERO
    ``confirmed_ids`` — the boundary that drives the ``subject=None`` guard."""

    def __init__(self, repo):
        self.repo = repo

    def synthesize(self, claims, gaps, repo):
        path, digest = _write_report(self.repo)
        return ReportArtifact(path=str(path), content_hash=digest, confirmed_ids=[])


def test_empty_confirmed_ids_boundary_subject_is_none(tmp_path):
    """EDGE / boundary: a real confirmed round whose report carries an EMPTY
    ``confirmed_ids`` list records confirmed-work with ``subject is None`` — the
    ``report.confirmed_ids[0] if report.confirmed_ids else None`` guard. Without
    it the index would raise an IndexError that ``drive()`` does not catch."""
    fe, _sha = _fe(tmp_path, synthesizer=_EmptyConfirmedIdsSynth(tmp_path))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].subject is None
    # it is still a real attested, file-witnessed unit despite the empty id list
    assert cw[-1].author == "claude" and cw[-1].witness["kind"] == "file"


def test_multiple_confirmed_claims_subject_is_first_id(tmp_path):
    """EDGE: with MULTIPLE confirmed sub-questions in one round, the confirmed-work
    subject is the FIRST confirmed claim id (the others are still cited in the
    report's ``confirmed_ids``). STEP-5's file only ever drove ONE sub-question."""
    # once=False so BOTH sub-questions in this single round are swept (the
    # sweeper's ``once`` counts CALLS, not rounds — once=True would starve q2).
    fe, _sha = _fe(tmp_path, sub_questions=("q1", "q2"), once=False)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    confirmed_subjects = [r.subject for r in rows
                          if r.event == "claim" and r.witness["status"] == "confirmed"]
    assert len(confirmed_subjects) == 2          # both sub-questions confirmed
    cw = [r for r in rows if r.event == "confirmed-work"]
    assert cw and cw[-1].subject == confirmed_subjects[0]


def test_confirmed_work_witness_writes_disk_sha(tmp_path):
    """HAPPY: the confirmed-work ``file`` witness ``sha256`` equals the sha256 of
    the report bytes the synthesizer WROTE to disk — the exact value the spine
    ``witness-ledgered`` gate re-derives. (STEP-5's happy test asserts only
    ``kind == 'file'``, never that the hash matches the on-disk bytes.)"""
    fe, _sha = _fe(tmp_path)
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    cw = [r for r in rows if r.event == "confirmed-work"]
    on_disk = (tmp_path / "RESEARCH.md").read_bytes()
    assert cw and cw[-1].witness["sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_garbage_head_sha_is_dry(tmp_path):
    """SAD: a committer that reports success with a GARBAGE (non-hex, unresolvable)
    head_sha must not green the loop — ``resolves_to_commit`` rejects it → a
    dry-round, no confirmed-work. (STEP-5's file uses ``deadbeef``, a valid hex
    prefix; this exercises a syntactically-garbage sha, a distinct input class.)"""
    fe, sha = _fe(tmp_path, committer=_FixedCommitter(
        CommitResult(ok=True, head_sha="not-a-real-sha!!", branch="research/x")))
    run = _run(tmp_path)
    fe.prepare(run)
    fe.run(run)
    rows = run.ledger.read()
    assert not any(r.event == "confirmed-work" for r in rows)
    assert rows[-1].event == "dry-round"
