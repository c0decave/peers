"""Phase 5 integration test — cleanliness gates end-to-end.

Exercises the full Phase 5 deliverable surface together: the three
hard cleanliness gates (Tasks 5.1 / 5.3 / 5.4), the reviewer-signed
``# JUSTIFIED`` and ``# SKIP-REASON`` escape mechanism (Task 5.2),
the hash-chain tamper-detection on ``.peers/justifications.log``,
and a representative sample of the five soft advisory gates (Task
5.5). Each scenario drives the gates through the public ``peers
-C <dir> run-check <name>`` entry point so the test reflects how
the substrate actually wires them up at runtime.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import pytest


def _run_check(name: str, project_dir: Path):
    """Invoke `peers -C <project_dir> run-check <name>` and capture I/O."""
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(project_dir), "run-check", name],
        capture_output=True, text=True,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _git_init_commit(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _attested_review(repo: Path, artifact: str, peer: str) -> None:
    """FU-2: the OTHER peer signs off via a substrate-attested peers-review
    commit (replaces the forgeable justifications.log reviewer field)."""
    _git(repo, "commit", "-q", "--allow-empty",
         "-m", f"peers-review: {artifact}\n\nLGTM")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "notes", "--ref=peers-attest", "add", "-f", "-m", peer, sha)


def _setup_clean_src(tmp_path: Path) -> Path:
    """Materialise a minimal clean src/ + tests/ tree for happy-path checks.

    The implementation has a real body (no `pass`, no stub return, no
    forbidden vocabulary); the tests cover happy/edge/sad cases without
    skip markers. This shape is meant to clear every Phase 5 gate.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.py").write_text("""def auth(token: str) -> bool:
    if not token:
        return False
    return token.startswith("valid-")
""")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text("""from src.auth import auth
def test_auth_happy_path():
    assert auth("valid-x")
def test_auth_edge_empty():
    assert not auth("")
def test_auth_sad_bad():
    assert not auth("nope")
""")
    return tmp_path


def test_clean_codebase_passes_all_hard_cleanliness(tmp_path):
    """All three Phase 5 hard gates exit 0 on a clean tree."""
    _setup_clean_src(tmp_path)
    for check in ["no_shortcut_markers", "no_empty_bodies", "no_skipped_tests"]:
        res = _run_check(check, tmp_path)
        assert res.returncode == 0, (
            f"{check} unexpectedly failed: {res.stdout}\n{res.stderr}"
        )


def test_polluted_codebase_fails_each_hard_gate(tmp_path):
    """Each hard gate fires distinctly on its dedicated violation."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "bad.py").write_text("""# TODO: implement
def stub():
    pass

class Concrete:
    def m(self):
        raise NotImplementedError
""")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("""import pytest
@pytest.mark.skip
def test_x(): pass
""")

    # Each hard gate fails distinctly with a diagnostic the operator
    # can act on (marker name, symbol name, or skip vocabulary).
    res = _run_check("no_shortcut_markers", tmp_path)
    assert res.returncode == 1
    assert "TODO" in res.stdout or "NotImplementedError" in res.stdout

    res = _run_check("no_empty_bodies", tmp_path)
    assert res.returncode == 1
    assert "stub" in res.stdout

    res = _run_check("no_skipped_tests", tmp_path)
    assert res.returncode == 1
    assert "test_x" in res.stdout or "skip" in res.stdout.lower()


def test_justified_marker_with_review_commit_passes(tmp_path):
    """FU-2: `# JUSTIFIED:` annotation + an independent, substrate-attested
    `peers-review: src/a.py` commit by the other peer => pass."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("# TODO: needs upstream  # JUSTIFIED: waits on issue 42\n")
    _git_init_commit(tmp_path)
    _attested_review(tmp_path, "src/a.py", "codex")

    res = _run_check("no_shortcut_markers", tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr


def test_skip_reason_with_review_commit_passes(tmp_path):
    """FU-2: `# SKIP-REASON:` annotation + an independent attested
    `peers-review: tests/test_a.py` commit => pass for skips."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("""import pytest
# SKIP-REASON: waits on upstream
@pytest.mark.skip(reason="x")
def test_x():
    pass
""")
    _git_init_commit(tmp_path)
    _attested_review(tmp_path, "tests/test_a.py", "codex")

    res = _run_check("no_skipped_tests", tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr


def test_justifications_chain_tampering_detected(tmp_path):
    """Mutating any past entry breaks the hash-chain at verify time."""
    from peers_ctl.justifications import (
        JustificationError,
        append_justification,
        verify_log_chain,
    )

    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    append_justification(plan_dir, "src/a.py", 1, "first", "codex")
    append_justification(plan_dir, "src/b.py", 2, "second", "claude")

    # Tamper with the *payload* of the first entry while leaving its
    # chain prefix intact -- this is what a malicious peer would do
    # to retroactively bless a different file.
    log = plan_dir / "justifications.log"
    lines = log.read_text().splitlines()
    lines[0] = lines[0].replace("src/a.py", "src/EVIL.py")
    log.write_text("\n".join(lines) + "\n")

    with pytest.raises(JustificationError):
        verify_log_chain(plan_dir)


def test_soft_gates_run_and_advise(tmp_path):
    """Soft gates run + exit 0 even when they find nothing to report."""
    _setup_clean_src(tmp_path)

    for check in ["no_stub_returns", "no_commented_code", "no_mock_in_impl"]:
        res = _run_check(check, tmp_path)
        # Soft gates are advisory: they MUST NOT block the loop.
        assert res.returncode == 0, f"{check} should be advisory: stdout={res.stdout}"


def test_phase5_clean_codebase_passes_soft_too(tmp_path):
    """End-to-end: clean tree clears every hard + soft gate we can name."""
    _setup_clean_src(tmp_path)
    for check in ["no_shortcut_markers", "no_empty_bodies", "no_skipped_tests",
                  "no_stub_returns", "no_commented_code", "no_mock_in_impl"]:
        res = _run_check(check, tmp_path)
        assert res.returncode == 0, f"{check}: stdout={res.stdout}"
