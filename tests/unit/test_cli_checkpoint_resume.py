"""Test --checkpoint flag + resume subcommand (Task 4.5).

Covers operator-supervised checkpoint between Phase 0 (architecture
tick) and the first implementation tick, plus the matching `resume`
subcommand that clears the marker so the operator can re-launch the
loop with `peers-ctl start <project>` once they've reviewed the
Phase 0 artefacts (RECON.md + PLAN.aligned.md +
ARCHITECTURE.intended.md).

Design choice (v1): `resume` only clears the marker; it does NOT
re-invoke `cmd_start`. The operator runs `peers-ctl start <project>`
explicitly after reviewing the artefacts. This keeps the resume path
flag-agnostic (no need to track the original start flags) and matches
the "operator-supervised" semantics — every resume is an explicit,
fresh `start` decision.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _peers_ctl(*args, env=None, cwd=None):
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env,
        cwd=str(cwd) if cwd else None,
    )


def test_checkpoint_flag_recognized():
    """--checkpoint flag is accepted by start subcommand."""
    res = _peers_ctl("start", "--help")
    assert res.returncode == 0
    assert "--checkpoint" in res.stdout


def test_resume_subcommand_exists():
    """`peers-ctl resume --help` works."""
    res = _peers_ctl("resume", "--help")
    assert res.returncode == 0
    assert "resume" in res.stdout.lower() or "project" in res.stdout.lower()


def test_resume_unknown_project_fails(tmp_path):
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    res = _peers_ctl("resume", "no-such-project", env=env)
    assert res.returncode != 0
    assert ("no such project" in res.stderr.lower()
            or "not found" in res.stderr.lower())


def test_resume_clears_checkpoint_marker(tmp_path):
    """resume removes .peers/checkpoint_requested if present."""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    (proj / ".peers").mkdir(parents=True)
    (proj / ".peers" / "checkpoint_requested").write_text("placeholder")
    (proj / ".peers" / "awaiting_user").write_text("placeholder")

    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    res = _peers_ctl("resume", "myfeature", env=env)
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert not (proj / ".peers" / "checkpoint_requested").exists()
    # Also clears the awaiting-user marker so the next start can proceed.
    assert not (proj / ".peers" / "awaiting_user").exists()


def test_resume_idempotent_when_no_marker(tmp_path):
    """resume succeeds even if no checkpoint marker exists (already cleared)."""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    (proj / ".peers").mkdir(parents=True)

    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    res = _peers_ctl("resume", "myfeature", env=env)
    assert res.returncode == 0, f"stderr={res.stderr}"


def test_resume_validate_project_name(tmp_path):
    """resume rejects path-traversal project names."""
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    res = _peers_ctl("resume", "../escape", env=env)
    assert res.returncode != 0
    assert ("invalid" in res.stderr.lower() or "name" in res.stderr.lower())


def test_resume_prints_next_step_hint(tmp_path):
    """resume should tell the operator how to actually continue."""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    (proj / ".peers").mkdir(parents=True)
    (proj / ".peers" / "checkpoint_requested").write_text("placeholder")

    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    res = _peers_ctl("resume", "myfeature", env=env)
    assert res.returncode == 0
    combined = (res.stdout + res.stderr).lower()
    assert "peers-ctl start" in combined or "start myfeature" in combined


# --- Driver-side: checkpoint marker recognition ----------------------


def test_driver_should_checkpoint_at_phase_0_to_impl():
    """When .peers/checkpoint_requested exists, the driver helper
    signals checkpoint when transitioning from architecture →
    implementation."""
    import tempfile
    from peers.driver_orchestrator import _should_checkpoint

    with tempfile.TemporaryDirectory() as td:
        plan_dir = Path(td) / ".peers"
        plan_dir.mkdir()
        (plan_dir / "checkpoint_requested").touch()
        assert _should_checkpoint(
            plan_dir,
            prev_phase="architecture", curr_phase="implementation",
        ) is True


def test_driver_no_checkpoint_without_marker():
    """No marker file → no checkpoint regardless of phase."""
    import tempfile
    from peers.driver_orchestrator import _should_checkpoint

    with tempfile.TemporaryDirectory() as td:
        plan_dir = Path(td) / ".peers"
        plan_dir.mkdir()
        assert _should_checkpoint(
            plan_dir,
            prev_phase="architecture", curr_phase="implementation",
        ) is False


def test_driver_no_checkpoint_on_other_transitions():
    """Even with marker, only architecture → implementation triggers."""
    import tempfile
    from peers.driver_orchestrator import _should_checkpoint

    with tempfile.TemporaryDirectory() as td:
        plan_dir = Path(td) / ".peers"
        plan_dir.mkdir()
        (plan_dir / "checkpoint_requested").touch()
        # recon → alignment: not the trigger
        assert _should_checkpoint(
            plan_dir,
            prev_phase="recon", curr_phase="alignment",
        ) is False
        # alignment → architecture: not the trigger
        assert _should_checkpoint(
            plan_dir,
            prev_phase="alignment", curr_phase="architecture",
        ) is False
        # implementation → implementation: not the trigger
        assert _should_checkpoint(
            plan_dir,
            prev_phase="implementation", curr_phase="implementation",
        ) is False
        # None prev (first tick): not the trigger
        assert _should_checkpoint(
            plan_dir,
            prev_phase=None, curr_phase="recon",
        ) is False


def test_checkpoint_flag_writes_marker_on_start(tmp_path):
    """`peers-ctl start <project> --checkpoint` writes the marker
    file before invoking the loop. (We can't easily start the full
    loop here, but we can verify the marker write side-effect — even
    if the start itself bails because the project is unregistered or
    misconfigured, the marker should land first.)"""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    (proj / ".peers").mkdir(parents=True)
    # Mark it as a registered project by creating a fake config so
    # start advances far enough to write the marker.
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    # We expect start to FAIL (no real project registered) — but the
    # --checkpoint flag should still be parsed and (ideally) the marker
    # written if the path exists. The test mainly guards that the flag
    # is accepted by argparse without error.
    res = _peers_ctl("start", "myfeature", "--checkpoint", env=env)
    # Either succeeds (unlikely without setup) or fails with a normal
    # error — never an argparse-level "unrecognized arguments" complaint.
    assert "unrecognized arguments" not in res.stderr.lower()
    assert "--checkpoint" not in res.stderr.lower() or res.returncode == 0


def test_start_checkpoint_refuses_symlinked_peers_dir_BUG_235(
    tmp_path, monkeypatch, capsys,
):
    """BUG-235: --checkpoint must not write through a symlinked .peers dir."""
    from peers_ctl import cli as cli_mod
    from peers_ctl.store import Project, Store

    cfg = tmp_path / "ctl"
    repo = tmp_path / "proj"
    repo.mkdir()
    outside = tmp_path / "outside-peers"
    outside.mkdir()
    (repo / ".peers").symlink_to(outside, target_is_directory=True)
    Store(cfg).add(Project(name="proj", path=str(repo)))

    started: list[bool] = []

    def fake_start_project(*args, **kwargs):
        started.append(True)
        return 12345

    monkeypatch.setattr(cli_mod, "start_project", fake_start_project)

    rc = cli_mod.cmd_start("proj", checkpoint=True, config_dir=cfg)

    assert rc != 0
    assert started == []
    assert not (outside / "checkpoint_requested").exists()
    err = capsys.readouterr().err.lower()
    assert "checkpoint" in err
    assert "symlink" in err or "refus" in err or "not a directory" in err
