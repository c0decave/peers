"""no-open-bug-reports convergence gate.

BUG-160: a corrupt or unreadable bug ledger should NOT silently
satisfy the gate. The previous code caught every exception from
`bug_hunt.summarize()`, printed `clean (ledger unavailable: ...)`,
and returned 0. This let a runtime error hide open blocking bugs at
convergence. The fix must fail closed: an exception means the gate
cannot verify cleanliness and must return non-zero.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


_CHECK = (
    Path(__file__).resolve().parents[2]
    / "src" / "peers" / "templates" / "modes" / "implement" / "checks"
    / "no_open_bug_reports.py"
)


def _new_repo(tmp_path: Path) -> Path:
    p = tmp_path / "r"
    p.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)
    (p / "seed").write_text("x")
    subprocess.run(["git", "add", "seed"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=p, check=True)
    return p


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHECK), str(repo)],
        capture_output=True, text=True,
    )


def test_passes_when_no_blocking_bugs(tmp_path):
    """Happy: empty ledger => clean."""
    repo = _new_repo(tmp_path)
    r = _run(repo)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "clean" in r.stdout


def test_fails_when_blocking_bug_open(tmp_path):
    """Sad: an open med-severity bug-report blocks convergence."""
    repo = _new_repo(tmp_path)
    body = (
        "BUG-001: test\n\n"
        "## Bug-Report\n"
        '{"id": "BUG-001", "severity": "med", '
        '"description": "x"}\n\n'
        "Peer: t\nBug-Report: BUG-001\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", body],
        check=True,
    )
    r = _run(repo)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "BUG-001" in r.stdout


def test_fails_closed_when_ledger_unreadable(tmp_path, monkeypatch):
    """BUG-160: when bug_hunt.summarize() raises, the gate must NOT
    return 0. Today it prints `clean (ledger unavailable: ...)` and
    returns 0, hiding any open bug from convergence."""
    # Drive the failure through a stub module: import the check
    # module in-process with a poisoned bug_hunt.summarize.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "no_open_bug_reports_under_test", _CHECK,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _boom(_repo):
        raise RuntimeError("ledger corrupt")
    monkeypatch.setattr(mod.bug_hunt, "summarize", _boom)
    rc = mod.main(str(tmp_path))
    assert rc != 0, "gate must fail closed when ledger unreadable"
