"""Test checkoff-by-other-peer check (Task 3.2)."""
from __future__ import annotations
import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import checkoff_by_other_peer


def _git(tmp_path: Path, *args: str, env=None):
    return subprocess.run(["git", "-C", str(tmp_path), *args],
                          capture_output=True, text=True, check=True, env=env)


def _init_git(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")


def _commit_as(tmp_path: Path, email: str, name: str, files: list[str], message: str) -> str:
    _git(tmp_path, "config", "user.email", email)
    _git(tmp_path, "config", "user.name", name)
    if files:
        _git(tmp_path, "add", *files)
    _git(tmp_path, "commit", "-q", "-m", message)
    return _git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def _write_plan(tmp_path: Path, body: str):
    (tmp_path / "PLAN.md").write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")


def test_no_checked_steps_passes(tmp_path, capsys):
    _init_git(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] todo\n  - touches: src/todo.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init")
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_checkoff_by_different_peer_passes(tmp_path, capsys):
    _init_git(tmp_path)
    # claude implements
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "step-1 impl")

    # codex reviews + checks off
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "codex@p.local", "codex", ["PLAN.md"], "step-1 reviewed")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0


def test_checkoff_by_same_peer_fails(tmp_path, capsys):
    _init_git(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "step-1 impl")

    # claude checks off own work
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "step-1 self-checkoff")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "claude@p.local" in out


def test_checkoff_without_touches_skipped(tmp_path, capsys):
    """A `trivial_step: true` step is exempt from the touches:
    requirement at parse time (Issue I4); the post-hoc gate has no
    files to anchor on either, so it must not flag a same-peer
    checkoff in that case."""
    _init_git(tmp_path)
    _write_plan(tmp_path, "- [ ] [STEP-1] trivial\n  - trivial_step: true\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "init")
    _write_plan(tmp_path, "- [x] [STEP-1] trivial\n  - trivial_step: true\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff but no touches")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 0  # passes — can't enforce without touches


def test_multi_step_one_violation_fails(tmp_path, capsys):
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "b.py").write_text("b")
    _write_plan(tmp_path, """- [ ] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/a.py", "src/b.py", "PLAN.md"], "init")

    # codex checks off step 1 (clean)
    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "codex@p.local", "codex", ["PLAN.md"], "checkoff step-1")

    # claude self-checks step 2 (violation)
    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [x] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "claude self-checkoff step-2")

    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out
    assert "STEP-1" not in out  # step-1 was clean


def test_no_plan_md_fails(tmp_path, capsys):
    _init_git(tmp_path)
    rc = checkoff_by_other_peer.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.md" in out
