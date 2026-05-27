"""Test plan-step-traceable check (Task 2.4)."""
from __future__ import annotations
import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import plan_step_traceable


def _git(tmp_path: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _init_git_with_commit(
    tmp_path: Path, file_changes: dict[str, str], message: str
) -> str:
    """Init git repo (if needed), write files, commit, return SHA."""
    if not (tmp_path / ".git").exists():
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "test@test.test")
        _git(tmp_path, "config", "user.name", "Test")
    for relpath, content in file_changes.items():
        f = tmp_path / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        _git(tmp_path, "add", relpath)
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD")


def _write_plan(tmp_path: Path, body: str) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")


def test_unchecked_steps_pass(tmp_path, capsys):
    _init_git_with_commit(tmp_path, {"x": "y"}, "init")
    _write_plan(
        tmp_path,
        "- [ ] [STEP-1] todo\n  - touches: src/a.py\n"
        "- [ ] [STEP-2] also todo\n  - touches: src/b.py\n",
    )
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out or "no checked" in out


def test_checked_step_with_valid_sha_passes(tmp_path, capsys):
    sha = _init_git_with_commit(
        tmp_path, {"src/auth.py": "def auth(): ..."}, "step-1 work"
    )
    short = sha[:7]
    _write_plan(
        tmp_path,
        f"- [x] [STEP-1] add auth ({short})\n  - touches: src/auth.py\n",
    )
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 0


def test_checked_step_missing_sha_fails(tmp_path, capsys):
    _init_git_with_commit(tmp_path, {"x": "y"}, "init")
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert (
        "missing" in out.lower()
        or "no sha" in out.lower()
        or "no commit" in out.lower()
    )


def test_checked_step_with_invalid_sha_fails(tmp_path, capsys):
    _init_git_with_commit(tmp_path, {"x": "y"}, "init")
    _write_plan(
        tmp_path,
        "- [x] [STEP-1] add auth (deadbeef)\n  - touches: src/auth.py\n",
    )
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "deadbeef" in out
    assert (
        "not found" in out.lower()
        or "invalid" in out.lower()
        or "unknown" in out.lower()
    )


def test_touches_match_passes(tmp_path, capsys):
    sha = _init_git_with_commit(tmp_path, {"src/auth.py": "..."}, "step-1")
    short = sha[:7]
    _write_plan(tmp_path, f"""- [x] [STEP-1] add auth ({short})
  - touches: src/auth.py
""")
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 0


def test_touches_mismatch_fails(tmp_path, capsys):
    sha = _init_git_with_commit(tmp_path, {"src/other.py": "..."}, "step-1")
    short = sha[:7]
    _write_plan(tmp_path, f"""- [x] [STEP-1] add auth ({short})
  - touches: src/auth.py
""")
    rc = plan_step_traceable.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert (
        "touches" in out.lower()
        or "doesn" in out.lower()
        or "src/auth.py" in out
    )
