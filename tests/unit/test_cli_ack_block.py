"""Test ack-block subcommand (Task 7.3).

`peers-ctl ack-block <project> STEP-N --reason "..."` transitions a
PLAN.md step from `[BLOCKED]` to `[BLOCKED-ACK]` and appends a
hash-chained audit entry to `.peers/blocks.log`. Errors when:
  * project name is invalid or no such project exists
  * STEP-N is not in the plan
  * STEP-N is not in the `[BLOCKED]` state
  * --reason is missing
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path


def _peers_ctl(*args, env=None):
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _setup_project(tmp_path: Path, plan_body: str) -> Path:
    proj = tmp_path / "projects" / "myfeature"
    proj.mkdir(parents=True)
    (proj / ".peers").mkdir()
    (proj / "PLAN.md").write_text(
        "# F\n"
        "## Meta\n"
        "surfaces: [cli]\n"
        "acceptance: pytest\n"
        "## Steps\n"
        f"{plan_body}"
    )
    return proj


def _make_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    return env


def test_ack_block_transitions_state(tmp_path):
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] needs API\n")
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "user decided to skip", env=env)
    assert res.returncode == 0, f"stderr={res.stderr}"
    plan = (proj / "PLAN.md").read_text()
    assert "[BLOCKED-ACK] [STEP-1]" in plan
    assert "[BLOCKED] [STEP-1]" not in plan


def test_ack_block_logs_to_blocks_log(tmp_path):
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] needs API\n")
    env = _make_env(tmp_path)
    _peers_ctl("ack-block", "myfeature", "STEP-1",
               "--reason", "user decided", env=env)
    log = (proj / ".peers" / "blocks.log").read_text()
    assert "STEP-1" in log
    assert "user decided" in log


def test_ack_block_unknown_step_fails(tmp_path):
    _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "myfeature", "STEP-99",
                     "--reason", "x", env=env)
    assert res.returncode != 0
    assert ("STEP-99" in res.stderr.lower()
            or "not found" in res.stderr.lower())


def test_ack_block_step_not_blocked_fails(tmp_path):
    _setup_project(tmp_path, "- [x] [STEP-1] already done\n")
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)
    assert res.returncode != 0
    assert ("not blocked" in res.stderr.lower()
            or "BLOCKED" in res.stderr)


def test_ack_block_requires_reason(tmp_path):
    _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "myfeature", "STEP-1", env=env)
    assert res.returncode != 0


def test_ack_block_unknown_project_fails(tmp_path):
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "noproject", "STEP-1",
                     "--reason", "x", env=env)
    assert res.returncode != 0
