"""Phase 6 integration test — honesty mechanisms end-to-end."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path



def _run_check(name: str, project_dir: Path):
    return subprocess.run(
        [sys.executable, "-m", "peers", "-C", str(project_dir), "run-check", name],
        capture_output=True, text=True,
    )


def test_blind_review_passes_with_both_notes(tmp_path):
    """Task 6.1 end-to-end via CLI."""
    # NB: blind_review requires >= 20 whitespace-separated tokens per file;
    # the prose below is sized to clear that threshold with a small margin.
    (tmp_path / "IMPLEMENTATION_NOTES.md").write_text(
        "Implemented JWT validation in src/auth.py with HS256 algorithm. "
        "Three test cases added covering happy path, expired token, and "
        "malformed input across the new module."
    )
    (tmp_path / "REVIEW_NOTES.md").write_text(
        "Reviewed src/auth.py: JWT validation with HS256. Tests cover happy, edge "
        "expired token, and malformed input. Implementation appears consistent "
        "with the stated module contract."
    )
    res = _run_check("blind_review", tmp_path)
    assert res.returncode == 0


def test_blind_review_fails_missing_review_notes(tmp_path):
    (tmp_path / "IMPLEMENTATION_NOTES.md").write_text(
        "twenty words of implementation notes here covering work done across "
        "several files of changes for the user feature in scope"
    )
    res = _run_check("blind_review", tmp_path)
    assert res.returncode == 1


def test_peer_role_resolution_alternates():
    """Task 6.2: implementer/reviewer alternation."""
    from peers.driver_orchestrator import _resolve_peer_role

    assert _resolve_peer_role("implement", "implementation", 3) == "implementer"
    assert _resolve_peer_role("implement", "implementation", 4) == "reviewer"
    assert _resolve_peer_role("implement", "implementation", 5) == "implementer"
    assert _resolve_peer_role("audit", "implementation", 3) == "normal"


def test_concerns_resolved_addressed_pass(tmp_path):
    """Task 6.3 end-to-end via CLI."""
    (tmp_path / "CONCERNS.md").write_text("""# Concerns
## Concern 1 — example
- raised-tick: 3
- raised-peer: codex
- description: a concern
- status: addressed (commit: abc1234)
""")
    res = _run_check("concerns_resolved", tmp_path)
    assert res.returncode == 0


def test_concerns_resolved_open_fails(tmp_path):
    (tmp_path / "CONCERNS.md").write_text("""# Concerns
## Concern 1 — unresolved
- raised-tick: 3
- raised-peer: codex
- description: x
- status: open
""")
    res = _run_check("concerns_resolved", tmp_path)
    assert res.returncode == 1


def test_honesty_audit_complete_passes(tmp_path):
    """Task 6.4 end-to-end via CLI."""
    (tmp_path / "HONESTY_AUDIT.md").write_text("""# Honesty Audit

## claude
### Weakest part
Token refresh path has no exponential backoff on auth-server failures during peak load conditions.

### Likely uncaught bug
Sessions don't get invalidated on password change because we use in-memory cache.

### Skipped or shortcut
No mutation testing was run since it was opt-in and we deferred to next iteration.

## codex
### Weakest part
Middleware regex for header parsing is brittle on multi-line headers and edge cases.

### Likely uncaught bug
Race between session expiry and refresh can produce 401 response even with fresh credentials.

### Skipped or shortcut
Coverage for rate-limit-exceeded path is missing but justified in PLAN.md.
""")
    res = _run_check("honesty_audit", tmp_path)
    assert res.returncode == 0


def test_honesty_audit_trivial_content_fails(tmp_path):
    (tmp_path / "HONESTY_AUDIT.md").write_text("""# Honesty Audit
## claude
### Weakest part
none
### Likely uncaught bug
none
### Skipped or shortcut
none
## codex
### Weakest part
none
### Likely uncaught bug
none
### Skipped or shortcut
none
""")
    res = _run_check("honesty_audit", tmp_path)
    assert res.returncode == 1


def test_two_phase_convergence_state_transitions():
    """Task 6.5: helper-level transition logic."""
    from peers.driver_orchestrator import _resolve_convergence_state

    # Phase A needs N=5 clean ticks
    assert _resolve_convergence_state("implement", "A", 4, 5, 2, 0) == "A"
    assert _resolve_convergence_state("implement", "A", 5, 5, 2, 0) == "B"

    # Phase B needs M=2 extra clean ticks with skeptic gates passing
    assert _resolve_convergence_state("implement", "B", 5, 5, 2, 1) == "B"
    assert _resolve_convergence_state("implement", "B", 5, 5, 2, 2) == "complete"

    # Other modes pass through
    assert _resolve_convergence_state("audit", "A", 5, 5, 2, 0) == "A"


def test_phase_6_combined_scenario(tmp_path):
    """All Phase 6 mechanisms passing on a 'ready-to-converge' project."""
    # Implementation + review notes (blind-review pass)
    (tmp_path / "IMPLEMENTATION_NOTES.md").write_text(
        "Implemented JWT auth + session store. Added 6 test cases across "
        "happy, edge, and sad paths for both modules with full coverage."
    )
    (tmp_path / "REVIEW_NOTES.md").write_text(
        "Reviewed JWT auth and session store implementations. Test coverage "
        "spans happy, edge, sad classes for both modules — looks complete."
    )

    # CONCERNS.md all addressed
    (tmp_path / "CONCERNS.md").write_text("""# Concerns
## Concern 1 — token refresh
- raised-tick: 4
- raised-peer: codex
- description: refresh path needs backoff
- status: addressed (commit: abc1234)
""")

    # HONESTY_AUDIT.md complete
    (tmp_path / "HONESTY_AUDIT.md").write_text("""# Honesty Audit
## claude
### Weakest part
Token refresh path could use exponential backoff under high load conditions.
### Likely uncaught bug
Sessions may not invalidate on password change in current implementation.
### Skipped or shortcut
Mutation testing deferred per PLAN scope.
## codex
### Weakest part
Header parsing regex is brittle on multi-line continuations.
### Likely uncaught bug
Race between session expiry and refresh under concurrent load.
### Skipped or shortcut
Rate-limit-exceeded path not fully covered yet.
""")

    # All 3 Phase B skeptic gates should pass
    for gate in ["blind_review", "concerns_resolved", "honesty_audit"]:
        res = _run_check(gate, tmp_path)
        assert res.returncode == 0, f"{gate} unexpectedly failed: {res.stdout}\n{res.stderr}"
