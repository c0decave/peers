"""peers run-check <name> — resolve and invoke a check script.

The shim resolves bare names against:
  1. <project>/.peers/checks/<name>.py
  2. each installed mode's templates/modes/<mode>/checks/<name>.py
With explicit mode prefix `mode:name`, only that mode is consulted.

Tests use subprocess-based invocation of `python -m peers.cli` to
exercise the real argparse + cmd_run_check path.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Locate this worktree's src/ so `python -m peers.cli` in the subprocess
# picks up the in-tree code rather than any globally-installed editable
# install pointed at a sibling worktree.
_SRC = Path(__file__).resolve().parents[2] / "src"


def _run_peers(target: Path, *extra_args: str,
               env_extra: dict[str, str] | None = None,
               ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{_SRC}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH") else str(_SRC)
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "peers.cli",
         "-C", str(target), *extra_args],
        capture_output=True, text=True, env=env,
    )


def _new_repo(tmp_path: Path) -> Path:
    p = tmp_path / "r"
    p.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)
    (p / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=p, check=True)
    return p


def test_run_check_resolves_from_project_peers_checks(tmp_path):
    """A check script in <project>/.peers/checks/foo.py resolves and
    runs; its exit code is forwarded."""
    repo = _new_repo(tmp_path)
    checks = repo / ".peers" / "checks"
    checks.mkdir(parents=True)
    (checks / "foo.py").write_text(
        "import sys\nprint('hello-from-foo')\nsys.exit(0)\n"
    )

    r = _run_peers(repo, "run-check", "foo",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "no-user")})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "hello-from-foo" in r.stdout


def test_run_check_resolves_from_builtin_mode(tmp_path):
    """`peers run-check verify_self_review` resolves to the audit
    mode's bundled script and runs it. With no handoff commit in
    history, verify_self_review exits 1 with a diagnostic — that's the
    contract we forward."""
    repo = _new_repo(tmp_path)

    r = _run_peers(repo, "run-check", "verify_self_review",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "no-user")})
    # No handoff commit => verify_self_review exits 1.
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "no handoff commit found" in r.stderr


def test_run_check_mode_qualified(tmp_path):
    """`peers run-check audit:verify_self_review` resolves to the
    audit-mode's version specifically (and ignores unrelated modes)."""
    repo = _new_repo(tmp_path)

    r = _run_peers(repo, "run-check", "audit:verify_self_review",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "no-user")})
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "no handoff commit found" in r.stderr


def test_run_check_unknown_name_lists_available(tmp_path):
    """An unknown check name => exit 1 + stderr lists available names."""
    repo = _new_repo(tmp_path)

    r = _run_peers(repo, "run-check", "nonexistent_check",
                   env_extra={"PEERS_MODES_DIR": str(tmp_path / "no-user")})
    assert r.returncode == 1
    assert "nonexistent_check" in r.stderr
    assert "available" in r.stderr.lower()
    # At minimum, the audit builtin checks should show up.
    assert "verify_self_review" in r.stderr


def test_run_check_ambiguous_without_prefix(tmp_path):
    """Two user modes that both ship `dup.py` => unqualified
    `peers run-check dup` is ambiguous and exits 1 with a suggestion
    to use `mode:name`."""
    repo = _new_repo(tmp_path)
    # Set up two user modes under PEERS_MODES_DIR that both define
    # dup.py. We give them unique names so they don't collide with
    # the audit/security builtins.
    user_modes = tmp_path / "user-modes"
    for mode_name in ("aaa", "bbb"):
        m = user_modes / mode_name
        (m / "checks").mkdir(parents=True)
        (m / "mode.yaml").write_text(
            f"name: {mode_name}\nversion: 1\ndescription: t\n"
        )
        (m / "goals.yaml").write_text("goals: []\n")
        (m / "checks" / "dup.py").write_text(
            f"import sys\nprint('from-{mode_name}')\nsys.exit(0)\n"
        )

    r = _run_peers(repo, "run-check", "dup",
                   env_extra={"PEERS_MODES_DIR": str(user_modes)})
    assert r.returncode == 1, (r.stdout, r.stderr)
    # Suggestion should mention the qualified form.
    assert "aaa:dup" in r.stderr
    assert "bbb:dup" in r.stderr
    assert "ambiguous" in r.stderr.lower()
