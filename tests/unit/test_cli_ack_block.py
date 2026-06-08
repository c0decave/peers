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


def test_ack_block_refuses_unsafe_blocks_log_before_rewriting_plan_BUG_237(
    tmp_path,
):
    """BUG-237: audit-log failures must not leave a partial ACK in PLAN.md."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    outside = tmp_path / "outside-peers"
    outside.mkdir()
    (outside / "blocks.log").write_text("deadbeefdeadbeef outside prev\n")
    peers_dir = proj / ".peers"
    peers_dir.rmdir()
    peers_dir.symlink_to(outside, target_is_directory=True)
    env = _make_env(tmp_path)

    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)

    assert res.returncode != 0, (res.stdout, res.stderr)
    assert "[BLOCKED] [STEP-1] x" in (proj / "PLAN.md").read_text()
    assert "[BLOCKED-ACK] [STEP-1]" not in (proj / "PLAN.md").read_text()
    assert "deadbeefdeadbeef outside prev\n" == (
        outside / "blocks.log"
    ).read_text()


def test_ack_block_append_failure_does_not_rewrite_plan_BUG_247(
    tmp_path,
    monkeypatch,
):
    """A late audit-log write failure must not leave an unaudited ACK."""
    from peers_ctl import cli

    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    config_dir = tmp_path / "ctl"
    cli._store(config_dir).add(cli.Project(name="myfeature", path=str(proj)))

    class FailingAuditLog:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, _text):
            raise OSError("simulated append failure")

    monkeypatch.setattr(
        cli,
        "open_text_in_dir_no_symlink",
        lambda *_args, **_kwargs: FailingAuditLog(),
    )

    rc = cli.cmd_ack_block(
        "myfeature",
        "STEP-1",
        "operator accepted blocker",
        config_dir=config_dir,
    )

    assert rc == 1
    plan = (proj / "PLAN.md").read_text()
    assert "[BLOCKED] [STEP-1] x" in plan
    assert "[BLOCKED-ACK] [STEP-1]" not in plan
    assert not (proj / ".peers" / "blocks.log").exists()


def test_ack_block_rejects_invalid_utf8_plan_without_traceback(tmp_path):
    """BUG-220 sad path: a peer-corrupted PLAN.md must be a clean CLI
    error, not an uncaught UnicodeDecodeError traceback."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    (proj / "PLAN.md").write_bytes(b"\xff\xfe not utf-8")
    env = _make_env(tmp_path)

    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)

    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "not valid UTF-8" in combined
    assert "Traceback" not in combined


def test_ack_block_refuses_symlinked_plan_on_read_BUG_220(tmp_path):
    """BUG-220 edge/security path: PLAN.md is project-controlled; refuse
    a symlink before parsing it instead of reading through and failing only
    at the later no-follow write."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    plan = proj / "PLAN.md"
    plan.unlink()
    target = tmp_path / "outside-plan.md"
    target.write_text(
        "# Outside\n## Meta\nsurfaces: [cli]\nacceptance: pytest\n"
        "## Steps\n- [BLOCKED] [STEP-1] outside\n"
    )
    plan.symlink_to(target)
    env = _make_env(tmp_path)

    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)

    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "cannot read" in combined
    lower = combined.lower()
    assert "too many levels" in lower or "symbolic link" in lower
    assert "[BLOCKED] [STEP-1] outside" in target.read_text()
    assert not (proj / ".peers" / "blocks.log").exists()


def test_ack_block_rejects_oversized_plan_BUG_220(tmp_path):
    """BUG-220 resource edge: don't slurp an unbounded peer-controlled
    PLAN.md into the host controller."""
    proj = _setup_project(tmp_path, "- [BLOCKED] [STEP-1] x\n")
    (proj / "PLAN.md").write_text("x" * (2 * 1024 * 1024 + 2))
    env = _make_env(tmp_path)

    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)

    combined = res.stdout + res.stderr
    assert res.returncode != 0
    assert "too large" in combined
    assert "Traceback" not in combined
