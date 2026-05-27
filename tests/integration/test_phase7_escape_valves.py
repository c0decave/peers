"""Phase 7 integration test — escape valves end-to-end."""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path



def _peers_ctl(*args, env=None):
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _run_check(name: str, project_dir: Path):
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(project_dir), "run-check", name],
        capture_output=True, text=True,
    )


def _make_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    return env


def _setup_project_with_blocked_step(tmp_path: Path) -> Path:
    proj = tmp_path / "projects" / "myfeature"
    proj.mkdir(parents=True)
    (proj / ".peers").mkdir()
    (proj / "PLAN.md").write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [x] [STEP-1] done thing
  - touches: src/done.py
- [BLOCKED] [STEP-2] needs external API access
  - touches: src/api.py
- [ ] [STEP-3] not yet
  - touches: src/later.py
""")
    return proj


def test_parser_recognizes_all_state_markers():
    """Task 7.1: state markers parse correctly."""
    from pathlib import Path
    import tempfile
    from peers_ctl.plan_parser import parse_plan

    with tempfile.TemporaryDirectory() as td:
        plan = Path(td) / "PLAN.md"
        plan.write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [x] [STEP-1] done
  - touches: src/a.py
- [BLOCKED] [STEP-2] blocked
  - touches: src/b.py
- [BLOCKED-ACK] [STEP-3] ack'd
  - touches: src/c.py
- [PARTIAL] [STEP-4] partial
  - touches: src/d.py
- [ ] [STEP-5] open
  - touches: src/e.py
""")
        p = parse_plan(plan)
        states = [s.state for s in p.steps]
        assert states == ["done", "blocked", "blocked-ack", "partial", "open"]


def test_blocked_step_fails_gate_before_ack(tmp_path):
    """Task 7.2: gate fails on unacknowledged blocked step."""
    proj = _setup_project_with_blocked_step(tmp_path)
    res = _run_check("no_unresolved_blocks", proj)
    assert res.returncode == 1
    assert "STEP-2" in res.stdout


def test_blocked_step_passes_gate_after_ack(tmp_path):
    """Task 7.2 + 7.3 round-trip: gate green after ack-block."""
    proj = _setup_project_with_blocked_step(tmp_path)
    env = _make_env(tmp_path)

    # Before ack: gate fails
    res = _run_check("no_unresolved_blocks", proj)
    assert res.returncode == 1

    # ack-block
    res = _peers_ctl("ack-block", "myfeature", "STEP-2",
                     "--reason", "external API not available", env=env)
    assert res.returncode == 0, f"ack-block failed: {res.stderr}"

    # After ack: gate passes
    res = _run_check("no_unresolved_blocks", proj)
    assert res.returncode == 0


def test_ack_block_audit_trail_persists(tmp_path):
    """Task 7.3: blocks.log captures the audit entry."""
    proj = _setup_project_with_blocked_step(tmp_path)
    env = _make_env(tmp_path)
    _peers_ctl("ack-block", "myfeature", "STEP-2",
               "--reason", "out of scope this sprint", env=env)

    log_path = proj / ".peers" / "blocks.log"
    assert log_path.exists()
    log = log_path.read_text()
    assert "STEP-2" in log
    assert "out of scope this sprint" in log


def test_ack_block_only_works_on_blocked_steps(tmp_path):
    """Task 7.3: cannot ack a non-blocked step."""
    _setup_project_with_blocked_step(tmp_path)
    env = _make_env(tmp_path)

    # STEP-1 is [x] done, not [BLOCKED]
    res = _peers_ctl("ack-block", "myfeature", "STEP-1",
                     "--reason", "x", env=env)
    assert res.returncode != 0
    assert "not blocked" in res.stderr.lower() or "BLOCKED" in res.stderr


def test_additive_duration_parsing():
    """Task 7.4: +Xh additive syntax."""
    from peers_ctl.cli import parse_runtime_duration

    absolute, additive_flag = parse_runtime_duration("6h")
    assert additive_flag is False
    assert absolute == 6 * 3600

    delta, additive_flag = parse_runtime_duration("+6h")
    assert additive_flag is True
    assert delta == 6 * 3600


def test_full_escape_valve_flow_simulated(tmp_path):
    """End-to-end: peer blocks, user acks, gate green, audit trail exists."""
    proj = _setup_project_with_blocked_step(tmp_path)
    env = _make_env(tmp_path)

    # 1. Gate fails initially
    assert _run_check("no_unresolved_blocks", proj).returncode == 1

    # 2. Multiple acks for traceability
    res1 = _peers_ctl("ack-block", "myfeature", "STEP-2",
                      "--reason", "third party blocker", env=env)
    assert res1.returncode == 0

    # 3. Gate now passes
    assert _run_check("no_unresolved_blocks", proj).returncode == 0

    # 4. PLAN.md transitioned correctly
    plan = (proj / "PLAN.md").read_text()
    assert "[BLOCKED-ACK] [STEP-2]" in plan
    assert "[BLOCKED] [STEP-2]" not in plan

    # 5. blocks.log preserves chain
    log = (proj / ".peers" / "blocks.log").read_text()
    assert "third party blocker" in log
    # Hash-chain prefix (16 hex chars)
    first_line = log.strip().splitlines()[0]
    prefix = first_line.split(" ", 1)[0]
    assert len(prefix) == 16
    int(prefix, 16)  # valid hex
