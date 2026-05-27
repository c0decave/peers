"""Phase 8 integration test — test-skepsis + drift prevention end-to-end."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path



def _run_check(name: str, project_dir: Path, env=None):
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(project_dir), "run-check", name],
        capture_output=True, text=True, env=env,
    )


def test_test_skeptic_review_clean_passes(tmp_path):
    """Task 8.1: TEST_SKEPSIS.md with concrete claims passes."""
    (tmp_path / "TEST_SKEPSIS.md").write_text("""# Test Skepsis

- tests/test_auth.py::test_jwt_validation - if I remove src/auth.py:42 (the signature check), test catches it because the assertion `validated == True` fails when signature is corrupt
- tests/test_session.py::test_expiry - if I remove src/session.py:88 (TTL check), test catches it because expired session returns valid status
""")
    res = _run_check("test_skeptic_review", tmp_path)
    assert res.returncode == 0


def test_test_skeptic_review_weasel_warns(tmp_path):
    """Task 8.1: weasel phrases like 'looks fine' get warned."""
    (tmp_path / "TEST_SKEPSIS.md").write_text("""# Test Skepsis

- tests/test_x.py - looks fine
""")
    res = _run_check("test_skeptic_review", tmp_path)
    # Soft gate: exit 0 with warning
    assert res.returncode == 0
    assert "looks fine" in res.stdout.lower() or "weasel" in res.stdout.lower() or "warn" in res.stdout.lower()


def test_architecture_coherent_aligned_passes(tmp_path):
    """Task 8.2: matching intended/actual architectures pass."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    intended = plan_dir / "ARCHITECTURE.intended.md"
    actual = tmp_path / "ARCHITECTURE.actual.md"

    content = """# Architecture

## Components
- AuthService: handles JWT
- SessionStore: redis

## Data Flow
- AuthService → SessionStore via RPC
"""
    intended.write_text(content)
    actual.write_text(content)

    res = _run_check("architecture_coherent", tmp_path)
    assert res.returncode == 0


def test_architecture_coherent_missing_intended_warns(tmp_path):
    """Task 8.2: missing intended file is warned (Phase 0 didn't run)."""
    (tmp_path / "ARCHITECTURE.actual.md").write_text("# Architecture\n## Components\n- X\n")
    res = _run_check("architecture_coherent", tmp_path)
    # Soft: exit 0 with warning
    assert res.returncode == 0
    out = res.stdout.lower()
    assert "intended" in out or "missing" in out or "warn" in out


def test_inter_step_coherence_few_steps_passes(tmp_path):
    """Task 8.3: ≤3 checked steps → no STITCH.md needed."""
    (tmp_path / "PLAN.md").write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [x] [STEP-1] one
- [x] [STEP-2] two
- [x] [STEP-3] three
""")
    res = _run_check("inter_step_coherence", tmp_path)
    assert res.returncode == 0


def test_inter_step_coherence_many_steps_without_stitch_warns(tmp_path):
    """Task 8.3: >3 checked steps without STITCH.md → warn."""
    (tmp_path / "PLAN.md").write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [x] [STEP-1] one
- [x] [STEP-2] two
- [x] [STEP-3] three
- [x] [STEP-4] four
- [x] [STEP-5] five
""")
    res = _run_check("inter_step_coherence", tmp_path)
    # Soft: exit 0 with warning
    assert res.returncode == 0
    out = res.stdout.lower()
    assert "stitch" in out or "warn" in out or "coherence" in out


def test_weasel_scan_clean_passes(tmp_path):
    """Task 8.4: weasel_scan on clean DELIVERY.md passes."""
    (tmp_path / "DELIVERY.md").write_text("""# Delivery

## [STEP-1] add auth
- Commit: abc1234
- Tests: tests/test_auth.py
- Justification: Implemented JWT validation with 5 test cases covering happy, edge, sad paths.
""")
    res = _run_check("weasel_scan", tmp_path)
    assert res.returncode == 0


def test_weasel_scan_detects_phrases(tmp_path):
    """Task 8.4: weasel_scan detects forbidden phrases."""
    (tmp_path / "DELIVERY.md").write_text("""# Delivery

## [STEP-1] auth
- Justification: should work in production, I think the edge case is handled
""")
    res = _run_check("weasel_scan", tmp_path)
    # Soft: exit 0 with warning
    assert res.returncode == 0
    out = res.stdout.lower()
    assert "should work" in out or "i think" in out or "weasel" in out


def test_honesty_audit_peer_gemini_opt_in_skip(tmp_path):
    """Task 8.4: gate skips if opt-in not enabled."""
    (tmp_path / "PLAN.md").write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [ ] [STEP-1] x
""")
    res = _run_check("honesty_audit_peer_gemini", tmp_path)
    # Skipped (no opt-in) → exit 0 clean
    assert res.returncode == 0
    assert "skipped" in res.stdout.lower() or "opt" in res.stdout.lower()


def test_phase_8_combined_clean(tmp_path):
    """All Phase 8 gates run cleanly on a well-formed project."""
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    (plan_dir / "ARCHITECTURE.intended.md").write_text("# Arch\n## Components\n- X\n")
    (tmp_path / "ARCHITECTURE.actual.md").write_text("# Arch\n## Components\n- X\n")
    (tmp_path / "PLAN.md").write_text("""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
- [x] [STEP-1] auth (abc1234)
""")
    (tmp_path / "DELIVERY.md").write_text("""# Delivery
## [STEP-1] auth
- Commit: abc1234
- Tests: tests/test_auth.py
- Justification: Implemented JWT with 5 test cases.
""")

    for gate in ["test_skeptic_review", "architecture_coherent", "inter_step_coherence",
                 "weasel_scan", "honesty_audit_peer_gemini", "mutation_sample",
                 "confidence_calibration"]:
        res = _run_check(gate, tmp_path)
        assert res.returncode == 0, f"{gate} failed: {res.stdout}\n{res.stderr}"
