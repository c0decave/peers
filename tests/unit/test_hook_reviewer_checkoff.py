"""Test reviewer-only-checkoff pre-commit hook (Task 3.1).

Killer Schicht-2 anti-cheat: implementer cannot checkoff their OWN step.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path


HOOK_SRC = (
    Path(__file__).parent.parent.parent
    / "src"
    / "peers"
    / "templates"
    / "modes"
    / "implement"
    / "hooks"
    / "pre-commit-reviewer-checkoff"
)


def _git(tmp_path: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _setup_repo_with_hook(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    # Avoid GPG signing / commit hooks from a global config
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "core.autocrlf", "false")
    # Install hook
    hooks_dir = tmp_path / ".git" / "hooks"
    hook = hooks_dir / "pre-commit"
    shutil.copy(HOOK_SRC, hook)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


def _commit_as(
    tmp_path: Path,
    email: str,
    name: str,
    files_to_add: list[str],
    message: str,
) -> str:
    """Commit with specific author identity. Returns SHA or '' on hook-reject."""
    _git(tmp_path, "config", "user.email", email)
    _git(tmp_path, "config", "user.name", name)
    if files_to_add:
        _git(tmp_path, "add", *files_to_add)
    res = _git(tmp_path, "commit", "-q", "-m", message, check=False)
    if res.returncode != 0:
        return ""  # hook rejected
    return _git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def _write_plan(tmp_path: Path, body: str) -> None:
    (tmp_path / "PLAN.md").write_text(body)


def test_no_plan_md_change_passes(tmp_path):
    _setup_repo_with_hook(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] a\n")
    sha1 = _commit_as(
        tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init"
    )
    assert sha1

    # Commit something else that doesn't touch PLAN.md
    src = tmp_path / "src" / "x.py"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    sha2 = _commit_as(
        tmp_path, "claude@p.local", "claude", ["src/x.py"], "src change"
    )
    assert sha2  # passes — no PLAN.md change


def test_checkoff_by_different_peer_allowed(tmp_path):
    _setup_repo_with_hook(tmp_path)
    # claude implements
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(
        tmp_path,
        "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n",
    )
    sha1 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["src/auth.py", "PLAN.md"],
        "step-1 impl",
    )
    assert sha1

    # codex (different identity) checks off
    _write_plan(
        tmp_path,
        "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n",
    )
    sha2 = _commit_as(
        tmp_path,
        "codex@p.local",
        "codex",
        ["PLAN.md"],
        "step-1 reviewed",
    )
    assert sha2  # allowed — different author


def test_checkoff_by_same_peer_rejected(tmp_path):
    _setup_repo_with_hook(tmp_path)
    # claude implements
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(
        tmp_path,
        "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n",
    )
    sha1 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["src/auth.py", "PLAN.md"],
        "step-1 impl",
    )
    assert sha1

    # claude tries to checkoff own work
    _write_plan(
        tmp_path,
        "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n",
    )
    sha2 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["PLAN.md"],
        "step-1 self-review",
    )
    assert sha2 == ""  # rejected


def test_checkoff_no_touches_allowed_with_warning(tmp_path):
    _setup_repo_with_hook(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] trivial\n")
    sha1 = _commit_as(
        tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init"
    )
    assert sha1

    _write_plan(tmp_path, "- [x] [STEP-1] trivial\n")
    sha2 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["PLAN.md"],
        "checkoff without touches",
    )
    # allowed since no touches: declared — can't enforce
    assert sha2


def test_multiple_checkoffs_one_violation_rejects_all(tmp_path):
    _setup_repo_with_hook(tmp_path)
    # claude implements src/a.py + src/b.py
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "b.py").write_text("b")
    _write_plan(
        tmp_path,
        """- [ ] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""",
    )
    sha1 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["src/a.py", "src/b.py", "PLAN.md"],
        "init both",
    )
    assert sha1

    # claude tries to checkoff both — should reject because both files last
    # touched by claude.
    _write_plan(
        tmp_path,
        """- [x] [STEP-1] a
  - touches: src/a.py
- [x] [STEP-2] b
  - touches: src/b.py
""",
    )
    sha2 = _commit_as(
        tmp_path,
        "claude@p.local",
        "claude",
        ["PLAN.md"],
        "checkoff both",
    )
    assert sha2 == ""  # rejected
