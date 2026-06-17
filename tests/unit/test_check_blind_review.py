"""Test blind-review check (Task 6.1)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import blind_review


def _setup(tmp_path: Path, impl_notes: str | None = None, review_notes: str | None = None, concerns: str | None = None):
    if impl_notes is not None:
        (tmp_path / "IMPLEMENTATION_NOTES.md").write_text(impl_notes)
    if review_notes is not None:
        (tmp_path / "REVIEW_NOTES.md").write_text(review_notes)
    if concerns is not None:
        (tmp_path / "CONCERNS.md").write_text(concerns)


def test_both_files_substantive_passes(tmp_path, capsys):
    _setup(tmp_path,
        impl_notes="Implemented JWT validation in src/auth.py. Token decoder uses HS256. Added 3 test cases covering happy, edge (expired), and sad (malformed) paths.",
        review_notes="Reviewed src/auth.py: JWT validation logic with HS256 algorithm. Test coverage includes happy path, expired token edge, and malformed input. Implementation looks sound.",
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 0


def test_missing_implementation_notes_fails(tmp_path, capsys):
    _setup(tmp_path, review_notes="some review notes here")
    rc = blind_review.main(str(tmp_path))
    assert rc == 1
    assert "IMPLEMENTATION_NOTES.md" in capsys.readouterr().out


def test_missing_review_notes_fails(tmp_path, capsys):
    _setup(tmp_path, impl_notes="some impl notes here")
    rc = blind_review.main(str(tmp_path))
    assert rc == 1
    assert "REVIEW_NOTES.md" in capsys.readouterr().out


def test_trivial_implementation_notes_fails(tmp_path, capsys):
    _setup(tmp_path, impl_notes="ok done", review_notes="long enough review notes here to pass the word count threshold easily oh yes more words for sure")
    rc = blind_review.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "trivial" in out.lower() or "too short" in out.lower() or "IMPLEMENTATION_NOTES" in out


def test_trivial_review_notes_fails(tmp_path, capsys):
    _setup(tmp_path,
        impl_notes="long enough notes here to pass the threshold for word count easily and confidently with more than twenty words",
        review_notes="ok",
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 1


def test_explicit_mismatch_marker_in_concerns_fails(tmp_path, capsys):
    _setup(tmp_path,
        impl_notes="implementation notes covering all the work that was done with sufficient word count",
        review_notes="review notes also covering the same topics with enough word count to be substantive",
        concerns="## Concerns\n[BLIND-REVIEW-MISMATCH] reviewer claims feature X not implemented, but it is\n",
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out or "mismatch" in out.lower()


def test_marker_referenced_in_prose_does_not_fail(tmp_path, capsys):
    """A CONCERNS entry that merely *mentions* the marker (documenting the
    protocol) must NOT be read as a filed mismatch. Only a marker that begins a
    line (a filed flag, per the reviewer prompt's 'add [marker] line') counts.
    Regression: a peer documenting 'if X disagrees, file `[BLIND-REVIEW-MISMATCH]`'
    inside a Concern's prose was false-failing the gate via a naive substring match.
    """
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact",
        concerns=(
            "## Concern 1 -- ruff E501 suppressed on frozen files\n"
            "- detail: I scoped the per-file ignore narrowly. A reviewer should\n"
            "  confirm this trade-off. If codex disagrees, file `[BLIND-REVIEW-MISMATCH]`.\n"
            "- status: addressed (commit: abc1234)\n"
        ),
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 0, capsys.readouterr().out


def test_backticked_marker_at_line_start_via_softwrap_does_not_fail(tmp_path, capsys):
    """The real regression: a prose sentence soft-wrapped so the *backticked*
    marker lands at the start of a continuation line. Inline code = a reference,
    never a filing — even at a line start. (Exact shape seen dogfooding.)"""
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact today",
        concerns=(
            "## Concern 1 -- ruff E501 suppressed on frozen files\n"
            "- detail: I scoped the ignore narrowly. A reviewer should confirm.\n"
            "  If codex disagrees, file\n"
            "  `[BLIND-REVIEW-MISMATCH]`.\n"          # marker backticked, at line start
            "- status: addressed (commit: abc1234)\n"
        ),
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 0, capsys.readouterr().out


def test_marker_filed_as_line_or_heading_or_list_still_fails(tmp_path, capsys):
    """A genuinely *filed* marker (its own line, or a heading/list-item flag)
    must still fail the gate — the fix narrows prose references, not filings."""
    impl = "implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it"
    review = "review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact today"
    for concerns in (
        "[BLIND-REVIEW-MISMATCH] reviewer says feature X is absent\n",
        "- [BLIND-REVIEW-MISMATCH] divergence in scene.py edge ordering\n",
        "## [BLIND-REVIEW-MISMATCH] timeline summary disagreement\n",
        "## Concerns\n[BLIND-REVIEW-MISMATCH] reviewer claims feature X not implemented\n",
        "  > [BLIND-REVIEW-MISMATCH] quoted filing still counts\n",
    ):
        _setup(tmp_path, impl_notes=impl, review_notes=review, concerns=concerns)
        rc = blind_review.main(str(tmp_path))
        assert rc == 1, f"should fail for filed marker: {concerns!r}\n{capsys.readouterr().out}"


def test_backticked_line_leading_filing_with_description_fires(tmp_path, capsys):
    """A genuine filing the reviewer writes line-leading but wrapped in backticks
    (the reviewer-prompt example uses backticks) MUST still fire. Regression: a
    global inline-code strip erased it -> false-NEGATIVE on a hard honesty gate."""
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact now",
        concerns="`[BLIND-REVIEW-MISMATCH]` reviewer says scene.py edge ordering diverges from notes\n",
    )
    assert blind_review.main(str(tmp_path)) == 1, capsys.readouterr().out


def test_filing_between_stray_backticks_on_other_lines_fires(tmp_path, capsys):
    """A genuine line-leading filing must fire even if other lines open/close an
    inline-code span around it. Regression: a doc-global strip joined the stray
    backticks across newlines and erased the filing between them (false-NEGATIVE)."""
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact now",
        concerns=(
            "I read `PLAN.md and the scene builder\n"
            "[BLIND-REVIEW-MISMATCH] timeline.win disagrees with the impl notes\n"
            "for the details` of the edge cases\n"
        ),
    )
    assert blind_review.main(str(tmp_path)) == 1, capsys.readouterr().out


def test_bold_wrapped_and_bare_eol_filings_fire(tmp_path, capsys):
    impl = "implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept it"
    review = "review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length for the artifact today now"
    for concerns in (
        "**[BLIND-REVIEW-MISMATCH]** reviewer disagrees about the export shape\n",
        "[BLIND-REVIEW-MISMATCH]\n",  # bare marker alone on its own line
    ):
        _setup(tmp_path, impl_notes=impl, review_notes=review, concerns=concerns)
        assert blind_review.main(str(tmp_path)) == 1, (
            f"should fire: {concerns!r}\n{capsys.readouterr().out}"
        )


def test_concerns_md_optional(tmp_path, capsys):
    """No CONCERNS.md -> pass (only the explicit marker would fail)."""
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length",
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 0
