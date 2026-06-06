"""Unit tests for `peers-ctl amend` subcommand (Task 1.4).

Covers the legitimate-acceptance-escape path: when the frozen acceptance
command needs to change mid-run (tests moved, command refinement, etc.),
operators run `peers-ctl amend <project> --acceptance <cmd> --reason
<text>`. This re-pins acceptance.sh, preserves 0444 mode, and appends a
hash-chained audit entry to contracts.log.

Validates:
* Project exists in the projects-root
* Project is in implement-mode (has .peers/contracts.sha)
* Both --acceptance and --reason are required
* contracts verify after the amendment
* Hash chain extends across multiple amendments
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Locate this worktree's src/ so `python -m peers_ctl` in the subprocess
# picks up the in-tree code (BUG-178: contracts module gained a
# chain-bound state suffix and seed entry — verify_contracts called from
# the test harness must agree with what the CLI wrote on disk).
_SRC = Path(__file__).resolve().parents[2] / "src"


def _peers_ctl_cmd() -> list[str]:
    return [sys.executable, "-m", "peers_ctl"]


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{_SRC}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH") else str(_SRC)
    )
    if extra:
        env.update(extra)
    return env


def _make_implement_project(tmp_path: Path, name: str = "myfeature") -> Path:
    """Helper: create a project via
    ``peers-ctl new --modes=implement --plan PLAN.md``.
    """
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# Feature\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        "acceptance: false\n"
        "## Steps\n"
        "- [ ] [STEP-1] do thing\n"
        "  - touches: src/thing.py\n"
    )
    projects = tmp_path / "projects"
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(projects),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    res = subprocess.run(
        _peers_ctl_cmd() + [
            "new", name, "--modes=implement", "--plan", str(plan),
        ],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, f"setup failed: {res.stderr}"
    return projects / name


def test_amend_updates_acceptance_and_logs(tmp_path, monkeypatch):
    proj = _make_implement_project(tmp_path)
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature",
                            "--acceptance", "pytest tests/new_path",
                            "--reason", "moved test directory"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, f"stderr={res.stderr}"
    # verify acceptance.sh contains the new command
    acc_text = (proj / ".peers" / "contracts" / "acceptance.sh").read_text()
    assert "pytest tests/new_path" in acc_text
    # verify contracts.log has the audit entry
    log_text = (proj / ".peers" / "contracts.log").read_text()
    assert "pytest tests/new_path" in log_text
    assert "moved test directory" in log_text


def test_amend_unknown_project_fails(tmp_path, monkeypatch):
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "no-such-project",
                            "--acceptance", "x",
                            "--reason", "y"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0
    assert ("no such project" in res.stderr.lower()
            or "not found" in res.stderr.lower())


def test_amend_non_implement_project_fails(tmp_path, monkeypatch):
    # Create a directory mimicking a project but WITHOUT contracts.sha
    proj = tmp_path / "projects" / "auditonly"
    (proj / ".peers").mkdir(parents=True)
    # no contracts.sha here
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "auditonly",
                            "--acceptance", "x",
                            "--reason", "y"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0
    assert ("implement" in res.stderr.lower()
            or "contracts" in res.stderr.lower())


def test_amend_missing_required_flags_fails(tmp_path, monkeypatch):
    _make_implement_project(tmp_path)
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    # missing --reason
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature", "--acceptance", "x"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0
    # missing --acceptance
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature", "--reason", "y"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0


def test_amend_verify_passes_after_change(tmp_path, monkeypatch):
    """Amendment leaves contracts in verifiable state."""
    proj = _make_implement_project(tmp_path)
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature",
                            "--acceptance", "pytest -q",
                            "--reason", "shorter output"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0
    # Independently verify contracts
    from peers_ctl.contracts import verify_contracts
    verify_contracts(proj / ".peers")  # must not raise


def test_amend_invalid_project_name_rejected(tmp_path, monkeypatch):
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
    })
    # path-traversal attempt
    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "../escape",
                            "--acceptance", "x", "--reason", "y"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode != 0
    assert "invalid" in res.stderr.lower() or "name" in res.stderr.lower()


def test_amend_hashchain_extends(tmp_path, monkeypatch):
    """Two consecutive amendments produce a chain with linked prefixes."""
    proj = _make_implement_project(tmp_path)
    env = _subprocess_env({
        "PEERS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    })

    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature",
                            "--acceptance", "pytest a",
                            "--reason", "first"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0

    res = subprocess.run(
        _peers_ctl_cmd() + ["amend", "myfeature",
                            "--acceptance", "pytest b",
                            "--reason", "second"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0

    log = (proj / ".peers" / "contracts.log").read_text()
    lines = [line for line in log.splitlines() if line.strip()]
    # init seed + two amends.
    assert len(lines) == 3
    # all lines have a 16-char hex prefix
    for line in lines:
        parts = line.split(" ", 1)
        assert len(parts[0]) == 16
        int(parts[0], 16)  # valid hex
