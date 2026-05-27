"""Test coverage-3class-delta check (Task 2.6)."""
from __future__ import annotations
import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import coverage_3class_delta


def _git(tmp_path: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _init_git(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@test.test")
    _git(tmp_path, "config", "user.name", "Test")


def _commit_test_file(
    tmp_path: Path, relpath: str, test_functions: list[str], message: str
) -> str:
    """Write a test file with the given test function names, commit, return SHA."""
    body = "\n\n".join(f"def {name}():\n    pass" for name in test_functions)
    f = tmp_path / relpath
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body + "\n")
    _git(tmp_path, "add", relpath)
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD")


def _write_plan(tmp_path: Path, body: str) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# F\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        "acceptance: pytest\n"
        "## Steps\n"
        f"{body}\n"
    )


def test_no_checked_steps_passes(tmp_path, capsys):
    _init_git(tmp_path)
    _commit_test_file(tmp_path, "tests/test_init.py", ["test_initial"], "init")
    _write_plan(tmp_path, "- [ ] [STEP-1] todo\n  - touches: src/todo.py\n")
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 0


def test_step_with_all_three_classes_passes(tmp_path, capsys):
    _init_git(tmp_path)
    sha = _commit_test_file(
        tmp_path,
        "tests/test_auth.py",
        [
            "test_auth_happy_path",  # happy
            "test_auth_edge_case_empty_token",  # edge
            "test_auth_sad_invalid_token",  # sad
        ],
        "step-1",
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] add auth ({sha[:7]})\n  - touches: src/auth.py\n")
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 0


def test_step_missing_sad_class_fails(tmp_path, capsys):
    _init_git(tmp_path)
    sha = _commit_test_file(
        tmp_path,
        "tests/test_auth.py",
        [
            "test_auth_happy_path",
            "test_auth_edge_empty_token",
        ],
        "step-1",
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] add auth ({sha[:7]})\n  - touches: src/auth.py\n")
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "sad" in out.lower()


def test_step_missing_all_three_classes_fails(tmp_path, capsys):
    _init_git(tmp_path)
    sha = _commit_test_file(
        tmp_path,
        "tests/test_auth.py",
        [
            "test_auth_something",
        ],
        "step-1",
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] add auth ({sha[:7]})\n  - touches: src/auth.py\n")
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out


def test_step_with_no_new_tests_fails(tmp_path, capsys):
    _init_git(tmp_path)
    # commit with no test changes
    src = tmp_path / "src/auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass\n")
    _git(tmp_path, "add", "src/auth.py")
    _git(tmp_path, "commit", "-q", "-m", "step-1 src only")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    _write_plan(tmp_path, f"- [x] [STEP-1] add auth ({sha[:7]})\n  - touches: src/auth.py\n")
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out


def test_step_without_sha_skipped(tmp_path, capsys):
    """Steps without commit_sha are not verifiable here; not a fail.

    plan-step-traceable enforces "must have SHA"; this gate's only
    concern is "with SHA, has 3 classes".
    """
    _init_git(tmp_path)
    _commit_test_file(tmp_path, "tests/test_init.py", ["test_initial"], "init")
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")  # no SHA annotation
    rc = coverage_3class_delta.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "not verifiable" in out.lower()
        or "skipped" in out.lower()
        or "no commit sha" in out.lower()
    )
