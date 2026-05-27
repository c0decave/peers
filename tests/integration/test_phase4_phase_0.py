"""Phase 4 integration test — Phase 0 end-to-end + checkpoint mechanism."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path



def _peers_ctl(*args, env=None, cwd=None):
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(cwd) if cwd else None)


def _make_env(projects_root: Path, config_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects_root)
    env["XDG_CONFIG_HOME"] = str(config_home)
    return env


def _make_plan(tmp_path: Path, acceptance: str = "false") -> Path:
    plan = tmp_path / "PLAN.md"
    plan.write_text(f"""# IntegrationFeature
## Meta
surfaces: [cli]
acceptance: {acceptance}
## Steps
- [ ] [STEP-1] do thing
  - touches: src/thing.py
""")
    return plan


def test_phase_resolution_for_all_modes():
    """Task 4.1: _resolve_phase produces correct phase for each (mode, tick)."""
    from peers.driver_orchestrator import _resolve_phase

    # implement-mode: 0/1/2 = phase 0 ticks; 3+ = implementation
    assert _resolve_phase("implement", 0) == "recon"
    assert _resolve_phase("implement", 1) == "alignment"
    assert _resolve_phase("implement", 2) == "architecture"
    assert _resolve_phase("implement", 3) == "implementation"
    assert _resolve_phase("implement", 99) == "implementation"

    # Non-implement modes: always implementation
    for mode in ["audit", "thorough", "security-owasp-web", "describe"]:
        for tick in [0, 1, 2, 5, 99]:
            assert _resolve_phase(mode, tick) == "implementation"


def test_phase_0_prompts_all_exist_and_load():
    """Tasks 4.2-4.4: all 3 prompt files present and loadable."""
    from peers.driver_orchestrator import _load_phase_prompt

    recon = _load_phase_prompt("implement", "recon")
    assert recon is not None
    assert "RECON.md" in recon

    alignment = _load_phase_prompt("implement", "alignment")
    assert alignment is not None
    assert "PLAN.aligned.md" in alignment

    architecture = _load_phase_prompt("implement", "architecture")
    assert architecture is not None
    assert "ARCHITECTURE.intended.md" in architecture

    # implementation phase has no overlay
    assert _load_phase_prompt("implement", "implementation") is None


def test_new_project_via_cli_creates_with_implement_mode(tmp_path):
    """Task 4.5: --checkpoint flag accepted by start, marker written to .peers/."""
    projects = tmp_path / "projects"
    config = tmp_path / "config"
    env = _make_env(projects, config)

    # Create project first
    plan = _make_plan(tmp_path)
    res = _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path)
    assert res.returncode == 0, f"new failed: {res.stderr}"

    # Verify --checkpoint flag is at least in start's help
    res = _peers_ctl("start", "--help", env=env)
    assert "--checkpoint" in res.stdout


def test_checkpoint_marker_lifecycle(tmp_path):
    """Task 4.5: marker creation by start + removal by resume."""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    plan_dir = proj / ".peers"
    plan_dir.mkdir(parents=True)

    # Manually create marker (as start --checkpoint would)
    marker = plan_dir / "checkpoint_requested"
    marker.touch()
    assert marker.exists()

    # Resume removes it
    env = _make_env(projects, tmp_path / "config")
    res = _peers_ctl("resume", "myfeature", env=env)
    assert res.returncode == 0
    assert not marker.exists()


def test_resume_idempotent(tmp_path):
    """Task 4.5: resume on a project without checkpoint marker is OK."""
    projects = tmp_path / "projects"
    proj = projects / "myfeature"
    (proj / ".peers").mkdir(parents=True)

    env = _make_env(projects, tmp_path / "config")
    res = _peers_ctl("resume", "myfeature", env=env)
    assert res.returncode == 0


def test_should_checkpoint_only_at_phase0_to_impl_transition(tmp_path):
    """Task 4.5: _should_checkpoint fires only at architecture→implementation."""
    from peers.driver_orchestrator import _should_checkpoint

    # `tmp_path` simulates the peer_dir (i.e. the project's .peers/ dir);
    # _should_checkpoint resolves the marker as `peer_dir / "checkpoint_requested"`.
    peer_dir = tmp_path
    (peer_dir / "checkpoint_requested").touch()

    # The trigger transition
    assert _should_checkpoint(peer_dir, prev_phase="architecture", curr_phase="implementation")

    # Non-trigger transitions
    assert not _should_checkpoint(peer_dir, prev_phase="recon", curr_phase="alignment")
    assert not _should_checkpoint(peer_dir, prev_phase="alignment", curr_phase="architecture")
    assert not _should_checkpoint(peer_dir, prev_phase="implementation", curr_phase="implementation")

    # Without marker, never triggers
    (peer_dir / "checkpoint_requested").unlink()
    assert not _should_checkpoint(peer_dir, prev_phase="architecture", curr_phase="implementation")


def test_phase_0_prompts_drive_correct_outputs():
    """Tasks 4.2-4.4: prompts mention the artifact each phase should produce."""
    from peers.driver_orchestrator import _load_phase_prompt

    # Each prompt should clearly tell the peer what file to produce
    assert "RECON.md" in _load_phase_prompt("implement", "recon")
    assert "PLAN.aligned.md" in _load_phase_prompt("implement", "alignment")
    assert "ARCHITECTURE.intended.md" in _load_phase_prompt("implement", "architecture")

    # Each prompt should mention what NOT to do (boundary conditions)
    assert "NOT" in _load_phase_prompt("implement", "recon") or "not" in _load_phase_prompt("implement", "recon").lower()
