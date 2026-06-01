"""Test no-open-bug-reports convergence gate."""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.templates.modes.implement.checks import no_open_bug_reports


def _git(repo: Path, *args: str):
    subprocess.run(["git", "-C", str(repo), *args],
                   capture_output=True, text=True, check=True)


def _init(repo: Path):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "p@local")
    _git(repo, "config", "user.name", "p")
    _git(repo, "config", "commit.gpgsign", "false")


def _empty_commit(repo: Path, message: str):
    subprocess.run(["git", "-C", str(repo), "commit", "-q",
                    "--allow-empty", "-m", message],
                   capture_output=True, text=True, check=True)


_REPORT = (
    'file BUG-001\n\n## Bug-Report\n{{\n  "id": "BUG-{n:03d}",\n'
    '  "severity": "{sev}"\n}}\n\nBug-Report: BUG-{n:03d}\n'
)


def test_clean_repo_passes(tmp_path, capsys):
    _init(tmp_path)
    _empty_commit(tmp_path, "init")
    assert no_open_bug_reports.main(str(tmp_path)) == 0
    assert "clean" in capsys.readouterr().out


def test_open_med_bug_fails(tmp_path, capsys):
    _init(tmp_path)
    _empty_commit(tmp_path, "init")
    _empty_commit(tmp_path, _REPORT.format(n=1, sev="med"))
    rc = no_open_bug_reports.main(str(tmp_path))
    assert rc == 1
    assert "BUG-001" in capsys.readouterr().out


def test_resolved_bug_passes(tmp_path, capsys):
    _init(tmp_path)
    _empty_commit(tmp_path, "init")
    _empty_commit(tmp_path, _REPORT.format(n=1, sev="high"))
    _empty_commit(tmp_path, "fix it\n\nBug-Resolves: BUG-001\n")
    assert no_open_bug_reports.main(str(tmp_path)) == 0


def test_low_severity_does_not_block(tmp_path, capsys):
    _init(tmp_path)
    _empty_commit(tmp_path, "init")
    _empty_commit(tmp_path, _REPORT.format(n=1, sev="low"))
    # below the `med` blocking threshold -> does not block convergence
    assert no_open_bug_reports.main(str(tmp_path)) == 0
