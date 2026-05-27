"""Test test-to-code-ratio soft cleanliness gate (Task 5.5.4).

For each `[x]` step with commit_sha, compare test-LOC added vs src-LOC
added in that commit. Warn if test-LOC < 0.5 * src-LOC. Steps with
`pure_refactor: true` are exempt.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import test_to_code_ratio


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


def _commit_files(
    tmp_path: Path, files: dict[str, str], message: str
) -> str:
    for relpath, content in files.items():
        f = tmp_path / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        _git(tmp_path, "add", relpath)
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD")


def _write_plan(tmp_path: Path, steps_body: str) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# F\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n"
        "## Steps\n" + steps_body + "\n"
    )


def test_no_checked_steps_passes(tmp_path, capsys):
    _init_git(tmp_path)
    _commit_files(tmp_path, {"src/x.py": "x = 1\n"}, "init")
    _write_plan(tmp_path, "- [ ] [STEP-1] todo\n  - touches: src/todo.py")
    rc = test_to_code_ratio.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_good_ratio_passes(tmp_path, capsys):
    """10 src LOC + 10 test LOC (ratio 1.0) is well above 0.5."""
    _init_git(tmp_path)
    src_body = "".join(f"x{i} = {i}\n" for i in range(10))
    test_body = "".join(f"def test_x{i}():\n    pass\n" for i in range(5))
    sha = _commit_files(
        tmp_path,
        {"src/m.py": src_body, "tests/test_m.py": test_body},
        "step-1",
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] add module ({sha[:7]})\n  - touches: src/m.py")
    rc = test_to_code_ratio.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_too_little_test_warns(tmp_path, capsys):
    """20 src LOC + 2 test LOC = ratio 0.1, well under 0.5."""
    _init_git(tmp_path)
    src_body = "".join(f"x{i} = {i}\n" for i in range(20))
    test_body = "def test_a():\n    pass\n"
    sha = _commit_files(
        tmp_path,
        {"src/big.py": src_body, "tests/test_big.py": test_body},
        "step-1",
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] big ({sha[:7]})\n  - touches: src/big.py")
    rc = test_to_code_ratio.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "STEP-1" in out


def test_pure_refactor_exempts_warning(tmp_path, capsys):
    """`pure_refactor: true` exempts a low test-to-code-ratio commit."""
    _init_git(tmp_path)
    src_body = "".join(f"x{i} = {i}\n" for i in range(20))
    sha = _commit_files(tmp_path, {"src/refactor.py": src_body}, "step-1")
    _write_plan(
        tmp_path,
        f"- [x] [STEP-1] refactor ({sha[:7]})\n"
        "  - touches: src/refactor.py\n"
        "  - pure_refactor: true",
    )
    rc = test_to_code_ratio.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_no_src_changes_passes(tmp_path, capsys):
    """A commit that adds tests but no src LOC is fine (ratio = inf)."""
    _init_git(tmp_path)
    _commit_files(tmp_path, {"src/x.py": "x = 1\n"}, "init")
    test_body = "def test_x():\n    pass\n"
    sha = _commit_files(
        tmp_path, {"tests/test_new.py": test_body}, "step-1"
    )
    _write_plan(tmp_path, f"- [x] [STEP-1] tests only ({sha[:7]})\n  - touches: tests/test_new.py")
    rc = test_to_code_ratio.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out
