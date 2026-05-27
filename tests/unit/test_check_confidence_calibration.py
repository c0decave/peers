"""Test confidence-calibration opt-in soft gate (Task 8.4).

Opt-in via PLAN.md Meta `confidence_calibration: true`. When opted in,
every `[x]` step must declare `- confidence: N/5` (N in {1..5});
N<4 produces a soft warning. Always exits 0.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import confidence_calibration


def _meta_plan(opt_in: bool, body: str = "") -> str:
    lines = ["# F\n", "\n", "## Meta\n", "surfaces: [cli]\n", "acceptance: pytest\n"]
    if opt_in:
        lines.append("confidence_calibration: true\n")
    lines.append("\n")
    lines.append("## Steps\n")
    lines.append(body)
    return "".join(lines)


def test_no_opt_in_skipped(tmp_path, capsys):
    """No confidence_calibration flag -- skipped."""
    (tmp_path / "PLAN.md").write_text(
        _meta_plan(opt_in=False, body="- [x] [STEP-1] do thing (abc1234)\n")
    )
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_no_plan_skipped(tmp_path, capsys):
    """No PLAN.md -- skipped."""
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_opt_in_with_high_confidence_clean(tmp_path, capsys):
    """All checked steps declare confidence >= 4/5 -- clean."""
    body = (
        "- [x] [STEP-1] do thing (abc1234)\n"
        "  - confidence: 5/5\n"
        "- [x] [STEP-2] do other (def5678)\n"
        "  - confidence: 4/5\n"
    )
    (tmp_path / "PLAN.md").write_text(_meta_plan(opt_in=True, body=body))
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_opt_in_missing_confidence_warns(tmp_path, capsys):
    """Checked step without a `confidence:` sub-attribute -- soft warn."""
    body = (
        "- [x] [STEP-1] do thing (abc1234)\n"
        "  - touches: src/foo.py\n"
    )
    (tmp_path / "PLAN.md").write_text(_meta_plan(opt_in=True, body=body))
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "STEP-1" in out


def test_opt_in_low_confidence_warns(tmp_path, capsys):
    """Confidence < 4/5 -- soft warn about low-confidence checkoff."""
    body = (
        "- [x] [STEP-1] do thing (abc1234)\n"
        "  - confidence: 2/5\n"
    )
    (tmp_path / "PLAN.md").write_text(_meta_plan(opt_in=True, body=body))
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "low-confidence" in out.lower() or "low confidence" in out.lower()


def test_opt_in_no_checked_steps_clean(tmp_path, capsys):
    """Opt-in but only [ ] steps -- clean (nothing to calibrate yet)."""
    body = "- [ ] [STEP-1] do thing\n"
    (tmp_path / "PLAN.md").write_text(_meta_plan(opt_in=True, body=body))
    rc = confidence_calibration.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()
