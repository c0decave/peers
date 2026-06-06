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
