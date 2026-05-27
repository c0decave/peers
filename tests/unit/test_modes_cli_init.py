from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Locate this worktree's src/ so `python -m peers.cli` in the subprocess
# picks up the in-tree code rather than any globally-installed editable
# install pointed at a sibling worktree.
_SRC = Path(__file__).resolve().parents[2] / "src"


def _run_init(target: Path, *extra_args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{_SRC}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH") else str(_SRC)
    )
    return subprocess.run(
        [sys.executable, "-m", "peers.cli",
         "-C", str(target), "init", *extra_args],
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


def test_init_modes_audit_installs_audit_artifacts(tmp_path):
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes=audit")
    assert r.returncode == 0, r.stderr
    checks = repo / ".peers" / "checks"
    for name in ("coverage_3class.py", "scan_secrets.py",
                 "deps_justified.py", "api_stable.py",
                 "no_regression.py", "diff_size_per_resolve.py"):
        assert (checks / name).is_file()


def test_init_modes_audit_security_stacks_both(tmp_path):
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes=audit,security")
    assert r.returncode == 0, r.stderr
    checks = repo / ".peers" / "checks"
    # audit checks
    assert (checks / "coverage_3class.py").is_file()
    # security checks
    assert (checks / "vuln_scan.py").is_file()
    assert (checks / "threat_model_present.py").is_file()
    # combined goals.yaml has IDs from both modes
    import yaml
    goals = yaml.safe_load((repo / ".peers" / "goals.yaml").read_text())
    ids = {g["id"] for g in goals["goals"]}
    assert "bug-hunt-clean" in ids        # from audit
    assert "vuln-scan-clean" in ids       # from security


def test_init_modes_unknown_errors_with_available_list(tmp_path):
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes=bogus")
    assert r.returncode != 0
    err = r.stderr + r.stdout
    assert "bogus" in err
    # The available modes list should be printed
    assert "audit" in err and "security" in err


def test_init_audit_templates_alias_still_works(tmp_path):
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--audit-templates")
    assert r.returncode == 0, r.stderr
    # deprecation note
    assert "deprecat" in (r.stderr.lower()) or "modes=audit" in r.stderr
    # audit checks present
    assert (repo / ".peers" / "checks" / "coverage_3class.py").is_file()


def test_init_modes_writes_modes_applied_audit_trail(tmp_path):
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes=audit,security")
    assert r.returncode == 0, r.stderr
    trail = repo / ".peers" / "modes-applied.txt"
    assert trail.is_file()
    text = trail.read_text()
    assert "audit" in text and "security" in text
    assert "sha256=" in text


def test_init_modes_empty_string_errors(tmp_path):
    """peers init --modes ',,,' errors (parsed to empty list), not silent no-op."""
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes", ", , ,")
    assert r.returncode == 2
    assert "empty list" in (r.stderr + r.stdout).lower()


def test_init_modes_unknown_does_not_leave_half_written_peers_dir(tmp_path):
    """When --modes contains an unknown mode, .peers/ must be untouched.
    Half-initialized state is worse than no state — the user can rerun
    fresh after fixing the typo."""
    repo = _new_repo(tmp_path)
    r = _run_init(repo, "--modes=audit,bogus,security")
    assert r.returncode != 0
    # .peers/ must NOT exist (or at minimum, not contain any of the files
    # that would normally be written by scaffold)
    peers_dir = repo / ".peers"
    if peers_dir.exists():
        # No goals.yaml, no config.yaml, no checks/
        leftover = [p.name for p in peers_dir.iterdir() if p.is_file()]
        assert not leftover, f"unexpected leftover files in .peers/: {leftover}"
        subdirs = [p.name for p in peers_dir.iterdir() if p.is_dir()]
        assert not subdirs, f"unexpected leftover subdirs in .peers/: {subdirs}"
