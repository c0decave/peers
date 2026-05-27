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


def test_concerns_md_optional(tmp_path, capsys):
    """No CONCERNS.md -> pass (only the explicit marker would fail)."""
    _setup(tmp_path,
        impl_notes="implementation notes here with at least twenty words to clear the threshold and be considered substantive content for the gate to accept",
        review_notes="review notes here with enough words to pass the threshold check that the gate enforces on the minimum content length",
    )
    rc = blind_review.main(str(tmp_path))
    assert rc == 0
