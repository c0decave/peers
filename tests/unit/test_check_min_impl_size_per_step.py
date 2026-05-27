"""Test min-impl-size-per-step soft cleanliness gate (Task 5.5.3).

For each `[x]` step with a commit_sha, count substantive LOC added in
that commit (excluding whitespace + comments). Warn if <10 LOC. Steps
with `trivial_step: true` are exempt from the warning.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import min_impl_size_per_step


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


def _commit_file(
    tmp_path: Path, relpath: str, content: str, message: str
) -> str:
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
    """No completed steps -> nothing to size-check."""
    _init_git(tmp_path)
    _commit_file(tmp_path, "src/x.py", "x = 1\n", "init")
    _write_plan(tmp_path, "- [ ] [STEP-1] todo\n  - touches: src/x.py")
    rc = min_impl_size_per_step.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_large_commit_passes(tmp_path, capsys):
    """A commit adding 15 substantive LOC under src/ is above threshold."""
    _init_git(tmp_path)
    body = "".join(f"x{i} = {i}\n" for i in range(15))
    sha = _commit_file(tmp_path, "src/big.py", body, "step-1")
    _write_plan(tmp_path, f"- [x] [STEP-1] add module ({sha[:7]})\n  - touches: src/big.py")
    rc = min_impl_size_per_step.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_tiny_commit_warns(tmp_path, capsys):
    """A commit with <10 substantive LOC under src/ triggers a soft warning."""
    _init_git(tmp_path)
    sha = _commit_file(tmp_path, "src/tiny.py", "x = 1\n", "step-1")
    _write_plan(tmp_path, f"- [x] [STEP-1] tiny ({sha[:7]})\n  - touches: src/tiny.py")
    rc = min_impl_size_per_step.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "STEP-1" in out


def test_trivial_step_exempts_warning(tmp_path, capsys):
    """`trivial_step: true` exempts a tiny commit from the warning."""
    _init_git(tmp_path)
    sha = _commit_file(tmp_path, "src/tiny.py", "x = 1\n", "step-1")
    _write_plan(
        tmp_path,
        f"- [x] [STEP-1] tiny ({sha[:7]})\n  - trivial_step: true",
    )
    rc = min_impl_size_per_step.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_whitespace_and_comments_excluded(tmp_path, capsys):
    """Blank lines and pure comments do not count toward the 10-LOC budget."""
    _init_git(tmp_path)
    body = (
        "# header comment\n"
        "\n"
        "# another comment\n"
        "\n"
        "x = 1\n"  # only one substantive LOC
    )
    sha = _commit_file(tmp_path, "src/fluff.py", body, "step-1")
    _write_plan(tmp_path, f"- [x] [STEP-1] mostly fluff ({sha[:7]})\n  - touches: src/fluff.py")
    rc = min_impl_size_per_step.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "STEP-1" in out
