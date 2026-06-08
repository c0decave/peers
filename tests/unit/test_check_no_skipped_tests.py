"""Test no-skipped-tests check (Task 5.4)."""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_skipped_tests


def _setup(tmp_path: Path, test_files: dict[str, str]) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name, body in test_files.items():
        (tests / name).write_text(body)
    return tmp_path


def test_clean_tests_pass(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": "def test_thing():\n    assert True\n"})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0


def test_pytest_skip_decorator_fails(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "test_x" in out or "skip" in out.lower()


def test_pytest_xfail_decorator_fails_BUG_170(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
@pytest.mark.xfail(reason="hides broken feature")
def test_x():
    assert False
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "pytest.mark.xfail" in out


def test_module_pytestmark_skip_fails_BUG_170(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
pytestmark = pytest.mark.skip(reason="hides broken file")

def test_x():
    assert False
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "module-level" in out


def test_module_pytestmark_xfail_list_fails_BUG_170(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
pytestmark = [pytest.mark.xfail(reason="hides broken file")]

def test_x():
    assert False
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "pytest.mark.xfail" in out


def test_unittest_skip_decorator_fails(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import unittest
class T(unittest.TestCase):
    @unittest.skip("x")
    def test_x(self):
        pass
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1


def test_pytest_skip_call_fails(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
def test_x():
    pytest.skip("nope")
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1


def test_signed_skip_passes(tmp_path, capsys):
    from peers_ctl.justifications import append_justification
    _setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: waits on upstream issue 42
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    # Signoff for the decorator line (line 3)
    append_justification(plan_dir, "tests/test_a.py", 3, "waits on upstream issue 42", "codex@p.local")
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0


def test_skip_reason_without_signoff_fails(tmp_path, capsys):
    _setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: handwave
@pytest.mark.skip
def test_x():
    pass
"""})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1


def test_skips_non_test_paths(tmp_path, capsys):
    """src/ has skip patterns but we only scan tests/ — should pass."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import pytest\n@pytest.mark.skip\ndef test_x(): pass\n")
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0


def test_forged_signoff_with_broken_chain_fails_closed_BUG_173(
    tmp_path, capsys,
):
    """A forged justification entry with bogus chain prefix must not
    waive a skip marker. The gate must verify the chain before consulting
    it; otherwise a peer can ship a hand-rolled log line that bypasses
    every signed-skip enforcement.
    """
    _setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: handwave
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    (plan_dir / "justifications.log").write_text(
        "0000000000000000 tests/test_a.py:3 attacker forged reason\n",
    )
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "chain" in out.lower() or "tamper" in out.lower()


# --- skip-baseline grandfathering (gate-scoping fix) ----------------------
#
# A fresh implement-mode run must not be blocked by skips that were already
# present in tests/ at run-start (inherited / pre-baseline skips). We mirror
# no_regression's baseline: snapshot the skip signatures once at run-start,
# then grandfather exactly those, while still failing every NEW unsigned skip.

_PREEXISTING = """import pytest


@pytest.mark.skip(reason="inherited")
def test_old():
    pass
"""

_BASELINE_FILE = "skip-baseline.txt"


def test_snapshot_writes_skip_baseline(tmp_path, capsys):
    """--snapshot writes the current skip signatures to
    .peers/skip-baseline.txt and exits 0 without flagging anything."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    (tmp_path / ".peers").mkdir()
    rc = no_skipped_tests.main(str(tmp_path), snapshot=True)
    assert rc == 0
    baseline = tmp_path / ".peers" / _BASELINE_FILE
    assert baseline.is_file()
    assert baseline.read_text().strip() != ""


def test_preexisting_skip_grandfathered_after_snapshot(tmp_path, capsys):
    """An unsigned skip captured at snapshot time passes a normal run."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    (tmp_path / ".peers").mkdir()
    assert no_skipped_tests.main(str(tmp_path), snapshot=True) == 0
    capsys.readouterr()
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0


def test_new_unsigned_skip_still_fails_after_snapshot(tmp_path, capsys):
    """A skip added AFTER the baseline snapshot is still a violation; the
    grandfathered one stays clean."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    (tmp_path / ".peers").mkdir()
    assert no_skipped_tests.main(str(tmp_path), snapshot=True) == 0
    capsys.readouterr()
    # Add a brand-new unsigned skip in a second file.
    (tmp_path / "tests" / "test_b.py").write_text(
        "import pytest\n@pytest.mark.skip\ndef test_new():\n    pass\n"
    )
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "test_b.py" in out
    assert "test_a.py" not in out  # grandfathered, not re-flagged


def test_grandfather_survives_prepended_line(tmp_path, capsys):
    """The baseline signature is line-number-independent: shifting the skip
    down by prepending a line must NOT un-grandfather it."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    (tmp_path / ".peers").mkdir()
    assert no_skipped_tests.main(str(tmp_path), snapshot=True) == 0
    capsys.readouterr()
    # Prepend a comment line, shifting the decorator's line number.
    shifted = "# unrelated edit elsewhere in the file\n" + _PREEXISTING
    (tmp_path / "tests" / "test_a.py").write_text(shifted)
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0


def test_no_baseline_file_is_backward_compatible(tmp_path, capsys):
    """With no skip-baseline.txt present, behaviour is unchanged: an
    unsigned skip still fails (empty baseline grandfathers nothing)."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1


def test_exit_calls_not_flagged_as_skip_BUG_011(tmp_path, capsys):
    """BUG-011 (eco-run): the textual xit/xdescribe matcher used substring
    `"xit(" in line`, so ANY line containing `exit(` (sys.exit, os._exit, or a
    function named ...xit) was falsely flagged as a JS-style skip. A skip-free
    file must pass."""
    _setup(tmp_path, {"test_a.py": (
        "import sys\n\n"
        "def test_clean_exit():\n"
        "    sys.exit(0)\n\n"
        "def test_calls_exit():\n"
        "    exit(1)\n\n"
        "def maxit(n):\n"
        "    return n\n"
    )})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 0, capsys.readouterr().out


def test_real_xit_still_detected_after_BUG_011_fix(tmp_path, capsys):
    """Regression guard: a genuine JS-style xit(...) / xdescribe(...) marker
    must still be caught after the word-boundary fix."""
    _setup(tmp_path, {"test_b.py": (
        "xit('skipped js test', () => {})\n"
        "xdescribe('skipped suite', () => {})\n"
    )})
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "xit" in out and "xdescribe" in out


def test_grandfather_revoked_when_guarded_body_changes(tmp_path, capsys):
    """HIGH-2 (adversarial review): the grandfather is bound to the skipped
    test's BODY, not just file+decorator+name. A peer must not be able to
    repurpose a baselined skip's identity to hide DIFFERENT (newly failing)
    code under the same decorator+name without a fresh SKIP-REASON+signoff."""
    _setup(tmp_path, {"test_a.py": _PREEXISTING})
    (tmp_path / ".peers").mkdir()
    assert no_skipped_tests.main(str(tmp_path), snapshot=True) == 0
    capsys.readouterr()
    # same file, same decorator, same function name — different body
    repurposed = (
        "import pytest\n\n\n@pytest.mark.skip(reason=\"inherited\")\n"
        "def test_old():\n    assert brand_new_untested_thing()  # repurposed\n"
    )
    (tmp_path / "tests" / "test_a.py").write_text(repurposed)
    rc = no_skipped_tests.main(str(tmp_path))
    assert rc == 1  # body changed -> identity no longer matches -> not grandfathered
