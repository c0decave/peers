"""Test test-skeptic-review soft gate (Task 8.1).

Soft gate -- emits findings to stdout but never blocks the loop. The
reviewer must write TEST_SKEPSIS.md with concrete claims per recent
test (specific src line + concrete failure reason). "Looks fine" /
"looks good" / "passes" boilerplate is rejected.

Always exits 0; findings are advisory.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import test_skeptic_review


def test_missing_file_passes(tmp_path, capsys):
    """No TEST_SKEPSIS.md (off-tick or new project) is fine -- exit 0, clean."""
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_empty_file_passes(tmp_path, capsys):
    """An empty TEST_SKEPSIS.md is treated like missing (periodic gate)."""
    (tmp_path / "TEST_SKEPSIS.md").write_text("")
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_concrete_entries_pass(tmp_path, capsys):
    """Entries with line references + concrete reasons (>10 words) pass."""
    body = (
        "# Test Skepsis Log\n"
        "\n"
        "- tests/unit/test_foo.py::test_happy - if I remove line 42 from "
        "src/foo.py the test catches it because the return value would "
        "fall back to None and the assertion on the dict key would raise.\n"
        "- tests/unit/test_bar.py::test_edge - if I remove line 17 from "
        "src/bar.py the boundary check at zero is skipped and the test "
        "asserting on the early-return value would now see the default.\n"
    )
    (tmp_path / "TEST_SKEPSIS.md").write_text(body)
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_looks_fine_entries_warn(tmp_path, capsys):
    """Boilerplate 'looks fine' / 'looks good' entries are flagged."""
    body = (
        "# Test Skepsis Log\n"
        "\n"
        "- tests/unit/test_foo.py::test_happy - looks fine\n"
        "- tests/unit/test_bar.py::test_edge - looks good to me, passes\n"
    )
    (tmp_path / "TEST_SKEPSIS.md").write_text(body)
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0  # soft -- still exits 0
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()


def test_entries_missing_line_refs_warn(tmp_path, capsys):
    """Entries without a `src/...:N` or `line N` reference are flagged."""
    body = (
        "# Test Skepsis Log\n"
        "\n"
        "- tests/unit/test_foo.py::test_happy - the function does the "
        "right thing because it returns the expected value for the given "
        "input which is what we want.\n"
    )
    (tmp_path / "TEST_SKEPSIS.md").write_text(body)
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()


def test_mixed_entries_report_only_bad(tmp_path, capsys):
    """Good entries pass; bad entries are listed individually."""
    body = (
        "# Test Skepsis\n"
        "\n"
        "- tests/unit/test_a.py::test_x - if I remove line 5 from "
        "src/a.py the early-return branch is skipped and the assertion "
        "on the cached value would see the recomputed default.\n"
        "- tests/unit/test_b.py::test_y - looks fine\n"
    )
    (tmp_path / "TEST_SKEPSIS.md").write_text(body)
    rc = test_skeptic_review.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "test_b.py" in out
