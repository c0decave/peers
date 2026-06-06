"""Phase 1 integration test — end-to-end peers-ctl new + amend round-trip.

Exercises Tasks 1.1 (parser), 1.2 (contracts), 1.3 (cli new --plan), 1.4 (cli amend)
as a real operator workflow.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest


def _peers_ctl(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "peers_ctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(cwd) if cwd else None)


_SRC = Path(__file__).resolve().parents[2] / "src"


def _make_env(projects_root: Path, config_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PEERS_PROJECTS_ROOT"] = str(projects_root)
    env["XDG_CONFIG_HOME"] = str(config_home)
    # make the subprocess pick up the in-tree contracts module
    # (chain-bound state suffix) so verify_contracts called from the test
    # harness agrees with what the CLI wrote on disk.
    env["PYTHONPATH"] = (
        f"{_SRC}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH") else str(_SRC)
    )
    return env


def _plan_cli(tmp_path: Path, acceptance: str = "false", e2e: str | None = None, surfaces: str = "[cli]") -> Path:
    plan = tmp_path / "PLAN.md"
    extras = f"\ne2e: {e2e}" if e2e else ""
    plan.write_text(f"""# IntegrationFeature

## Meta
surfaces: {surfaces}
acceptance: {acceptance}{extras}

## Steps
- [ ] [STEP-1] do thing
  - touches: src/thing.py
""")
    return plan


def test_new_then_inspect_full_filesystem_layout(tmp_path):
    """Verify all files created in expected locations with correct modes + content."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    config = tmp_path / "config"
    env = _make_env(projects, config)

    res = _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path)
    assert res.returncode == 0, f"stderr={res.stderr}"

    proj = projects / "myfeature"
    plan_dir = proj / ".peers"

    # Live PLAN.md (operator-editable, mode 0644)
    assert (proj / "PLAN.md").is_file()
    assert (proj / "PLAN.md").stat().st_mode & 0o777 == 0o644 or (proj / "PLAN.md").stat().st_mode & 0o200  # writable

    # PLAN.original.md (frozen, 0444)
    assert (plan_dir / "PLAN.original.md").is_file()
    assert (plan_dir / "PLAN.original.md").stat().st_mode & 0o777 == 0o444

    # contracts.sha (0644, JSON)
    assert (plan_dir / "contracts.sha").is_file()
    sha_data = json.loads((plan_dir / "contracts.sha").read_text())
    assert "acceptance.sh" in sha_data
    assert "PLAN.original.md" in sha_data
    assert "e2e.sh" not in sha_data  # no e2e in plan

    # contracts/acceptance.sh (frozen, 0444)
    acc = plan_dir / "contracts" / "acceptance.sh"
    assert acc.is_file()
    assert acc.stat().st_mode & 0o777 == 0o444
    assert "false" in acc.read_text()

    # SHA matches
    assert sha_data["acceptance.sh"] == sha256(acc.read_bytes()).hexdigest()
    assert sha_data["PLAN.original.md"] == sha256((plan_dir / "PLAN.original.md").read_bytes()).hexdigest()


def test_new_with_e2e_creates_e2e_contract(tmp_path):
    """When surfaces:[web] + e2e: declared, e2e.sh is frozen too."""
    plan = _plan_cli(tmp_path, acceptance="false", e2e="playwright test", surfaces="[web]")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    res = _peers_ctl("new", "webfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path)
    assert res.returncode == 0, f"stderr={res.stderr}"

    plan_dir = projects / "webfeature" / ".peers"
    e2e_path = plan_dir / "contracts" / "e2e.sh"
    assert e2e_path.is_file()
    assert e2e_path.stat().st_mode & 0o777 == 0o444
    assert "playwright test" in e2e_path.read_text()

    sha_data = json.loads((plan_dir / "contracts.sha").read_text())
    assert "e2e.sh" in sha_data
    assert sha_data["e2e.sh"] == sha256(e2e_path.read_bytes()).hexdigest()


def test_new_then_amend_then_verify_passes(tmp_path):
    """Amend re-pins SHA so verify_contracts passes after."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    assert _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path).returncode == 0
    assert _peers_ctl("amend", "myfeature", "--acceptance", "pytest tests/", "--reason", "moved tests", env=env, cwd=tmp_path).returncode == 0

    # Independently verify
    from peers_ctl.contracts import verify_contracts
    verify_contracts(projects / "myfeature" / ".peers")  # no exception


def test_amend_does_not_touch_plan_original(tmp_path):
    """PLAN.original.md SHA stays the same across amendments."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    assert _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path).returncode == 0

    plan_dir = projects / "myfeature" / ".peers"
    sha_before = json.loads((plan_dir / "contracts.sha").read_text())["PLAN.original.md"]

    assert _peers_ctl("amend", "myfeature", "--acceptance", "x", "--reason", "y", env=env, cwd=tmp_path).returncode == 0

    sha_after = json.loads((plan_dir / "contracts.sha").read_text())["PLAN.original.md"]
    assert sha_before == sha_after


def test_hashchain_extends_across_multiple_amendments(tmp_path):
    """3 sequential amendments produce a 3-entry hash-chained log."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    assert _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path).returncode == 0

    for i in range(3):
        res = _peers_ctl(
            "amend", "myfeature",
            "--acceptance", f"pytest run_{i}",
            "--reason", f"iteration {i}",
            env=env, cwd=tmp_path,
        )
        assert res.returncode == 0, f"amend {i}: {res.stderr}"

    log_text = (projects / "myfeature" / ".peers" / "contracts.log").read_text()
    lines = [line for line in log_text.splitlines() if line.strip()]
    # init seed + three amends.
    assert len(lines) == 4

    # Each line: <16-hex-prefix> <iso8601> <event>[: <body>] | state: <hash>
    seen_prefixes = []
    for entry in lines:
        prefix, rest = entry.split(" ", 1)
        assert len(prefix) == 16
        int(prefix, 16)  # valid hex
        seen_prefixes.append(prefix)
        assert "| state:" in rest
    # Init entry is event=init, amend entries carry the amend payload.
    assert "init |" in lines[0]
    for amend_line in lines[1:]:
        assert "amend acceptance:" in amend_line
        assert "reason:" in amend_line

    # Prefixes are deterministic, distinct
    assert len(set(seen_prefixes)) == 4


def test_tampering_after_amend_detected(tmp_path):
    """After amend, tampering with acceptance.sh fails verify_contracts."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    assert _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path).returncode == 0
    assert _peers_ctl("amend", "myfeature", "--acceptance", "pytest", "--reason", "init", env=env, cwd=tmp_path).returncode == 0

    # Tamper: forcibly chmod + rewrite acceptance.sh
    plan_dir = projects / "myfeature" / ".peers"
    acc = plan_dir / "contracts" / "acceptance.sh"
    acc.chmod(0o600)
    acc.write_text("#!/bin/sh\necho cheating\n")

    from peers_ctl.contracts import verify_contracts, ContractsMismatch
    with pytest.raises(ContractsMismatch, match="tampered"):
        verify_contracts(plan_dir)


def test_plan_md_live_copy_matches_original_initially(tmp_path):
    """Live PLAN.md content == PLAN.original.md content right after `new`."""
    plan = _plan_cli(tmp_path, acceptance="false")
    projects = tmp_path / "projects"
    env = _make_env(projects, tmp_path / "config")

    assert _peers_ctl("new", "myfeature", "--modes=implement", "--plan", str(plan), env=env, cwd=tmp_path).returncode == 0

    proj = projects / "myfeature"
    live = (proj / "PLAN.md").read_text()
    original = (proj / ".peers" / "PLAN.original.md").read_text()
    assert live == original
