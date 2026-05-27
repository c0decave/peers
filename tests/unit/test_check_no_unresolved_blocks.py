"""Test no-unresolved-blocks check (Task 7.2).

Companion to the Task 7.1 step-state markers in plan_parser.py.
At convergence, no step may be in `[BLOCKED]` state without an
explicit operator acknowledgement (which rewrites the marker to
`[BLOCKED-ACK]` via `peers-ctl ack-block`, Task 7.3).
"""
from __future__ import annotations

from pathlib import Path

from peers.templates.modes.implement.checks import no_unresolved_blocks


def _write_plan(tmp_path: Path, steps_lines: list[str]) -> Path:
    """Write a minimal valid PLAN.md to tmp_path with the given step lines."""
    plan = tmp_path / "PLAN.md"
    body = "\n".join(steps_lines)
    plan.write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")
    return plan


def test_no_steps_blocked_passes(tmp_path, capsys):
    """A plan with only done/open steps passes -- no blocks at all."""
    _write_plan(tmp_path, [
        "- [x] [STEP-1] done",
        "  - touches: src/a.py",
        "- [ ] [STEP-2] open",
        "  - touches: src/b.py",
    ])
    rc = no_unresolved_blocks.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_blocked_step_fails(tmp_path, capsys):
    """A single `[BLOCKED]` step (no -ACK) fails the gate."""
    _write_plan(tmp_path, [
        "- [BLOCKED] [STEP-1] blocked",
        "  - touches: src/x.py",
    ])
    rc = no_unresolved_blocks.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out


def test_blocked_ack_passes(tmp_path, capsys):
    """`[BLOCKED-ACK]` means operator has signed off -- gate passes."""
    _write_plan(tmp_path, [
        "- [BLOCKED-ACK] [STEP-1] ack'd",
        "  - touches: src/x.py",
    ])
    rc = no_unresolved_blocks.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_mixed_blocked_ack_and_blocked_fails(tmp_path, capsys):
    """Only the un-acknowledged blocked step is reported; ack'd one is silent."""
    _write_plan(tmp_path, [
        "- [BLOCKED-ACK] [STEP-1] ok",
        "  - touches: src/a.py",
        "- [BLOCKED] [STEP-2] not ok",
        "  - touches: src/b.py",
    ])
    rc = no_unresolved_blocks.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out
    assert "STEP-1" not in out


def test_missing_plan_md_fails(tmp_path, capsys):
    """No PLAN.md at all is treated as a hard fail, matching sibling gates."""
    rc = no_unresolved_blocks.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.md not found" in out
