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


_SRC = Path(__file__).resolve().parents[2] / "src"


def _make_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    # Point at the in-tree code so symlink-safe edits land in the
    # subprocess (peers_ctl is installed from a non-editable snapshot).
    env["PYTHONPATH"] = (
        f"{_SRC}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH") else str(_SRC)
    )
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


def test_ack_block_refuses_symlinked_blocks_log_BUG_165(tmp_path):
    """BUG-165: a symlink at .peers/blocks.log must NOT redirect the
    operator's audit-log append. The legacy code did Path.open("a"),
    which followed the symlink and wrote outside the project."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    # Plant a symlink at the blocks.log path that points OUTSIDE the
    # project (a target the operator did not consent to write).
    target = tmp_path / "evil_target.log"
    target.write_text("untouched\n")
    log_path = proj / ".peers" / "blocks.log"
    log_path.symlink_to(target)
    env = _make_env(tmp_path)
    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)
    # Must fail with a no-follow / refusing-symlink diagnostic.
    assert res.returncode != 0, (res.stdout, res.stderr)
    # The evil target must not have been appended to.
    assert target.read_text() == "untouched\n"
    err = (res.stderr + res.stdout).lower()
    assert "symlink" in err or "refus" in err or "non-regular" in err


def test_ack_block_refuses_symlinked_blocks_log_on_read_BUG_165(tmp_path):
    """BUG-165 read-side: with an existing chain file, the prev-chain
    read also went through Path.open() which follows symlinks. Same
    no-follow refusal must apply on the read path."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    # Create a legitimate previous entry by running once normally —
    # then replace the file with a symlink before the second run.
    env = _make_env(tmp_path)
    _peers_ctl("ack-block", "myfeature", "STEP-1",
               "--reason", "first", env=env)
    log_path = proj / ".peers" / "blocks.log"
    target = tmp_path / "evil_target2.log"
    target.write_text("attacker-controlled prev chain\n")
    log_path.unlink()
    log_path.symlink_to(target)
    # Now re-block STEP-1 so we can ack again.
    (proj / "PLAN.md").write_text(
        (proj / "PLAN.md").read_text()
        .replace("[BLOCKED-ACK]", "[BLOCKED]")
    )
    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "second", env=env)
    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "attacker-controlled prev chain\n" == target.read_text()
