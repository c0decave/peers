"""Test driver Phase 0 state machine (Task 4.1).

Covers `_resolve_phase(mode_name, tick_number)` — the small additive
helper that decides which phase string the driver writes to state.json
at the start of each tick — plus the wiring that puts that phase
string into state.json + the mode auto-detection from
`.peers/modes-applied.txt`.

Implement-mode gets 3 fixed prep ticks (recon → alignment →
architecture) before normal implementation; every other mode runs
in "implementation" from tick 0 (backward-compat).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from peers.driver_orchestrator import (
    OrchestratorDriver,
    _detect_mode_name,
    _resolve_phase,
)
from peers.peer_spec import PeerSpec


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "x").write_text("x")
    _git(path, "add", "x")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _specs() -> list[PeerSpec]:
    return [
        PeerSpec(name="claude", tool="claude", argv=("true",),
                 prompt_mode="stdin"),
        PeerSpec(name="codex", tool="codex", argv=("true",),
                 prompt_mode="stdin"),
    ]


def _make_driver(repo: Path) -> OrchestratorDriver:
    return OrchestratorDriver(
        repo=repo, peer_dir=repo / ".peers",
        goals=[], peer_specs=_specs(),
    )


def test_audit_mode_always_implementation_phase():
    assert _resolve_phase("audit", 0) == "implementation"
    assert _resolve_phase("audit", 1) == "implementation"
    assert _resolve_phase("audit", 99) == "implementation"


def test_thorough_mode_always_implementation_phase():
    assert _resolve_phase("thorough", 0) == "implementation"
    assert _resolve_phase("thorough", 5) == "implementation"


def test_security_mode_always_implementation_phase():
    """security-mode + sub-variants must not pick up Phase 0 prep
    ticks (they aren't writing PLAN-driven feature code)."""
    assert _resolve_phase("security", 0) == "implementation"
    assert _resolve_phase("security-owasp-web", 0) == "implementation"


def test_implement_mode_tick_0_is_recon():
    assert _resolve_phase("implement", 0) == "recon"


def test_implement_mode_tick_1_is_alignment():
    assert _resolve_phase("implement", 1) == "alignment"


def test_implement_mode_tick_2_is_architecture():
    assert _resolve_phase("implement", 2) == "architecture"


def test_implement_mode_tick_3_plus_is_implementation():
    assert _resolve_phase("implement", 3) == "implementation"
    assert _resolve_phase("implement", 5) == "implementation"
    assert _resolve_phase("implement", 99) == "implementation"


def test_unknown_mode_defaults_to_implementation():
    """Backward compat: unknown modes (custom user-modes, empty
    string when mode detection fails) fall through to
    implementation — never accidentally turn on Phase 0 for
    something other than first-party implement-mode."""
    assert _resolve_phase("custom-user-mode", 0) == "implementation"
    assert _resolve_phase("", 0) == "implementation"


def test_phase_resolution_is_pure():
    """No hidden state — same inputs always return same outputs.
    Important because the driver calls this on every tick start."""
    for _ in range(3):
        assert _resolve_phase("implement", 0) == "recon"
        assert _resolve_phase("implement", 1) == "alignment"
        assert _resolve_phase("implement", 2) == "architecture"
        assert _resolve_phase("implement", 3) == "implementation"
        assert _resolve_phase("audit", 0) == "implementation"


# ---------- mode auto-detection from .peers/modes-applied.txt ----------

def test_detect_mode_name_missing_trail_returns_empty(tmp_path: Path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    assert _detect_mode_name(peer_dir) == ""


def test_detect_mode_name_implement_only(tmp_path: Path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  implement       v1  sha256=abc\n"
    )
    assert _detect_mode_name(peer_dir) == "implement"


def test_detect_mode_name_composed_modes_returns_empty(tmp_path: Path):
    """v1 implement-mode is documented as standalone; if it appears
    alongside other modes we refuse to enable Phase 0 (safety: avoid
    surprising users who stacked modes deliberately)."""
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  audit           v1  sha256=def\n"
        "2026-05-26T12:34:56+00:00  implement       v1  sha256=abc\n"
    )
    assert _detect_mode_name(peer_dir) == ""


def test_detect_mode_name_audit_only(tmp_path: Path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  audit           v1  sha256=def\n"
    )
    assert _detect_mode_name(peer_dir) == ""


def test_detect_mode_name_document_only(tmp_path: Path):
    peer_dir = tmp_path / ".peers"
    peer_dir.mkdir()
    (peer_dir / "modes-applied.txt").write_text(
        "2026-06-01T12:34:56+00:00  document        v1  sha256=abc\n"
    )
    assert _detect_mode_name(peer_dir) == "document"


def test_document_mode_runs_in_implementation_phase_no_phase0(tmp_path: Path):
    # document is NOT a Phase-0 mode — every tick is "implementation"
    # (its task brief loads via prompts/implementation.md, not a Phase-0 prelude).
    assert _resolve_phase("document", 0) == "implementation"
    assert _resolve_phase("document", 2) == "implementation"
    assert _resolve_phase("document", 99) == "implementation"


# ---------- driver integration: state.json carries `phase` -----------

def test_driver_record_phase_writes_state_field_audit_mode(tmp_path: Path):
    repo = _make_repo(tmp_path / "r")
    drv = _make_driver(repo)
    # No modes-applied.txt → mode_name="" → always implementation
    assert drv.mode_name == ""
    state: dict = {"iteration": 0}
    drv._record_phase(state)
    assert state["phase"] == "implementation"
    state["iteration"] = 5
    drv._record_phase(state)
    assert state["phase"] == "implementation"


def test_driver_record_phase_writes_state_field_implement_mode(
    tmp_path: Path,
):
    repo = _make_repo(tmp_path / "r")
    peer_dir = repo / ".peers"
    peer_dir.mkdir()
    (peer_dir / "modes-applied.txt").write_text(
        "2026-05-26T12:34:56+00:00  implement       v1  sha256=abc\n"
    )
    drv = _make_driver(repo)
    assert drv.mode_name == "implement"
    # Walk through the Phase 0 schedule and check state.json gets stamped:
    state: dict = {"iteration": 0}
    drv._record_phase(state)
    assert state["phase"] == "recon"
    state["iteration"] = 1
    drv._record_phase(state)
    assert state["phase"] == "alignment"
    state["iteration"] = 2
    drv._record_phase(state)
    assert state["phase"] == "architecture"
    state["iteration"] = 3
    drv._record_phase(state)
    assert state["phase"] == "implementation"
