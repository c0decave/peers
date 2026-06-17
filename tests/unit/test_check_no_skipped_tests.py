"""Test no-skipped-tests check (Task 5.4)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import no_skipped_tests


def _setup(tmp_path: Path, test_files: dict[str, str]) -> Path:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name, body in test_files.items():
        (tests / name).write_text(body)
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _git_setup(tmp_path: Path, test_files: dict[str, str]) -> Path:
    """_setup + git init + initial commit (so FU-2 review commits resolve)."""
    proj = _setup(tmp_path, test_files)
    _git(proj, "init", "-q")
    _git(proj, "config", "commit.gpgsign", "false")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "init")
    return proj


def _attested_review(repo: Path, artifact: str, peer: str) -> str:
    """The OTHER peer signs off via a substrate-attested peers-review commit."""
    _git(repo, "commit", "-q", "--allow-empty",
         "-m", f"peers-review: {artifact}\n\nLGTM")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "notes", "--ref=peers-attest", "add", "-f", "-m", peer, sha)
    return sha


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


def test_signed_skip_passes_with_review_commit(tmp_path, capsys):
    # FU-2: the SKIP-REASON skip is forgiven once an independent peer (codex)
    # signs off on tests/test_a.py via a substrate-attested peers-review commit.
    proj = _git_setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: waits on upstream issue 42
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    _attested_review(proj, "tests/test_a.py", "codex")
    rc = no_skipped_tests.main(str(proj))
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


def test_forged_log_entry_does_not_bless_FU_2(tmp_path, capsys):
    """FU-2 (supersedes BUG-173): a justifications.log entry no longer waives a
    skip — the forgeable, agent-authored log is not the mechanism. Only a
    substrate-attested peers-review commit forgives a SKIP-REASON skip, so a
    hand-written entry bypasses nothing.
    """
    proj = _git_setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: handwave
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    plan_dir = proj / ".peers"
    plan_dir.mkdir()
    (plan_dir / "justifications.log").write_text(
        "0000000000000000 tests/test_a.py:3 attacker forged reason\n",
    )
    rc = no_skipped_tests.main(str(proj))
    assert rc == 1
    assert "test_a.py" in capsys.readouterr().out


def test_multi_author_laundering_does_not_bless_skip_FU_2(tmp_path, capsys):
    # CRITICAL (adversarial review): claude authors the skip + SKIP-REASON;
    # codex makes a trivial edit elsewhere (last editor); claude self-reviews.
    # The guard must exclude the SKIP LINE's author (claude), not the last
    # editor (codex), so the self-review is rejected.
    proj = _git_setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: deferred
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "claude", init)
    # codex appends a trailing line — does NOT touch the skip (line 3) or
    # SKIP-REASON (line 2)
    with (proj / "tests" / "test_a.py").open("a") as f:
        f.write("\n# trailing\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "codex trivial")
    edit = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", edit)
    _attested_review(proj, "tests/test_a.py", "claude")  # skip author self-reviews
    rc = no_skipped_tests.main(str(proj))
    assert rc == 1
    assert "test_a.py" in capsys.readouterr().out


def test_coeditor_who_did_not_author_skip_can_review_FU_2(tmp_path, capsys):
    # complement (not over-strict): codex edited a non-skip line, so codex can
    # review claude's skip.
    proj = _git_setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: deferred
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "claude", init)
    with (proj / "tests" / "test_a.py").open("a") as f:
        f.write("\n# trailing\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "codex trivial")
    edit = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", edit)
    _attested_review(proj, "tests/test_a.py", "codex")
    rc = no_skipped_tests.main(str(proj))
    assert rc == 0


def test_self_review_does_not_bless_skip_FU_2(tmp_path, capsys):
    """FU-2 sad: the test file's own author cannot self-bless its skip — a
    peers-review commit attested to the author (codex) is excluded."""
    proj = _git_setup(tmp_path, {"test_a.py": """import pytest
# SKIP-REASON: self
@pytest.mark.skip(reason="x")
def test_x():
    pass
"""})
    init = _git(proj, "rev-parse", "HEAD").strip()
    _git(proj, "notes", "--ref=peers-attest", "add", "-f", "-m", "codex", init)
    _attested_review(proj, "tests/test_a.py", "codex")  # self-review
    rc = no_skipped_tests.main(str(proj))
    assert rc == 1
    assert "test_a.py" in capsys.readouterr().out


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
