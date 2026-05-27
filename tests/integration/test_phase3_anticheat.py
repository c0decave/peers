"""Phase 3 integration test — anti-cheat end-to-end."""
from __future__ import annotations
import shutil
import stat
import subprocess
import sys
from pathlib import Path

HOOK_SRC = Path(__file__).parent.parent.parent / "src" / "peers" / "templates" / "modes" / "implement" / "hooks" / "pre-commit-reviewer-checkoff"


def _git(tmp_path: Path, *args, check=True, env=None):
    return subprocess.run(["git", "-C", str(tmp_path), *args],
                          capture_output=True, text=True, check=check, env=env)


def _setup_repo_with_hook(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    shutil.copy(HOOK_SRC, hook)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _commit_as(tmp_path: Path, email: str, name: str, files: list[str], message: str) -> str | None:
    _git(tmp_path, "config", "user.email", email)
    _git(tmp_path, "config", "user.name", name)
    if files:
        _git(tmp_path, "add", *files)
    res = _git(tmp_path, "commit", "-q", "-m", message, check=False)
    if res.returncode != 0:
        return None
    return _git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def _write_plan(tmp_path: Path, body: str):
    (tmp_path / "PLAN.md").write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")


def _run_check(check: str, project_dir: Path):
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(project_dir), "run-check", check],
        capture_output=True, text=True,
    )


def test_hook_blocks_self_checkoff(tmp_path):
    """Task 3.1: hook rejects when same author tries to check off own work."""
    _setup_repo_with_hook(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    sha1 = _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "impl")
    assert sha1

    # claude tries self-checkoff
    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    sha2 = _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff")
    assert sha2 is None  # hook rejected


def test_hook_allows_peer_checkoff(tmp_path):
    """Task 3.1: hook allows when other peer checks off."""
    _setup_repo_with_hook(tmp_path)
    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "impl")

    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    sha2 = _commit_as(tmp_path, "codex@p.local", "codex", ["PLAN.md"], "reviewed")
    assert sha2 is not None  # allowed


def test_post_hoc_gate_catches_self_checkoff_without_hook(tmp_path):
    """Task 3.2: backup gate detects self-checkoff in committed history (no hook installed)."""
    # NO hook — pre-existing repo without it
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    src = tmp_path / "src" / "auth.py"
    src.parent.mkdir(parents=True)
    src.write_text("def auth(): pass")
    _write_plan(tmp_path, "- [ ] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/auth.py", "PLAN.md"], "impl")

    _write_plan(tmp_path, "- [x] [STEP-1] add auth\n  - touches: src/auth.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff")

    res = _run_check("checkoff_by_other_peer", tmp_path)
    assert res.returncode == 1
    assert "STEP-1" in res.stdout


def test_delivery_report_complete_with_all_steps(tmp_path):
    """Task 3.3: complete DELIVERY.md passes."""
    _git(tmp_path, "init", "-q")
    _write_plan(
        tmp_path,
        "- [x] [STEP-1] add auth\n"
        "  - touches: src/auth.py\n"
        "- [x] [STEP-2] add session\n"
        "  - touches: src/session.py\n",
    )
    (tmp_path / "DELIVERY.md").write_text("""# Delivery

## [STEP-1] add auth
- **Commit:** abc1234
- **Tests:** tests/test_auth.py
- **Justification:** Implemented JWT validation with 3 test cases.

## [STEP-2] add session
- **Commit:** def5678
- **Tests:** tests/test_session.py
- **Justification:** Redis session store with TTL.
""")
    res = _run_check("delivery_report_complete", tmp_path)
    assert res.returncode == 0


def test_delivery_report_missing_step_fails(tmp_path):
    """Task 3.3: missing step in DELIVERY.md fails."""
    _git(tmp_path, "init", "-q")
    _write_plan(
        tmp_path,
        "- [x] [STEP-1] add auth\n"
        "  - touches: src/auth.py\n"
        "- [x] [STEP-2] add session\n"
        "  - touches: src/session.py\n",
    )
    (tmp_path / "DELIVERY.md").write_text("""# Delivery

## [STEP-1] add auth
- **Commit:** abc1234
- **Tests:** tests/test_auth.py
- **Justification:** Done.
""")  # STEP-2 missing
    res = _run_check("delivery_report_complete", tmp_path)
    assert res.returncode == 1
    assert "STEP-2" in res.stdout


def test_hook_plus_gate_belt_and_suspenders(tmp_path):
    """Hook prevents new violations; gate catches historical violations.

    Scenario: hook installed late after some violations already committed.
    Hook rejects new attempts; gate detects old violations.
    """
    # Phase A: no hook, claude commits violations
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    _write_plan(tmp_path, "- [ ] [STEP-1] a\n  - touches: src/a.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/a.py", "PLAN.md"], "impl")
    _write_plan(tmp_path, "- [x] [STEP-1] a\n  - touches: src/a.py\n")
    _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff (old)")

    # Phase B: hook installed
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    shutil.copy(HOOK_SRC, hook)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    # claude tries another self-checkoff with a new step
    (tmp_path / "src" / "b.py").write_text("b")
    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [ ] [STEP-2] b
  - touches: src/b.py
""")
    _commit_as(tmp_path, "claude@p.local", "claude", ["src/b.py", "PLAN.md"], "impl step-2")

    _write_plan(tmp_path, """- [x] [STEP-1] a
  - touches: src/a.py
- [x] [STEP-2] b
  - touches: src/b.py
""")
    sha = _commit_as(tmp_path, "claude@p.local", "claude", ["PLAN.md"], "self-checkoff step-2")
    assert sha is None  # hook caught new attempt

    # Gate detects old violation (STEP-1)
    res = _run_check("checkoff_by_other_peer", tmp_path)
    assert res.returncode == 1
    assert "STEP-1" in res.stdout
