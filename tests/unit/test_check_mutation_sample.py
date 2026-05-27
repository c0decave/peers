"""Test mutation-sample opt-in soft gate (Task 8.4).

Stub placeholder for future mutmut/cosmic-ray integration. Opt-in via
PLAN.md Meta `mutation_testing: true`. When opted in, prints a clear
"not yet implemented" notice; when not, skips. Always exits 0.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import mutation_sample


def _meta_plan(mutation_testing: bool | None = None) -> str:
    lines = ["# F\n", "\n", "## Meta\n", "surfaces: [cli]\n", "acceptance: pytest\n"]
    if mutation_testing is not None:
        lines.append(f"mutation_testing: {str(mutation_testing).lower()}\n")
    lines.append("\n")
    lines.append("## Steps\n")
    lines.append("- [ ] [STEP-1] do thing\n")
    return "".join(lines)


def test_no_opt_in_skipped(tmp_path, capsys):
    """No mutation_testing flag -- gate skips with exit 0."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(mutation_testing=None))
    rc = mutation_sample.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_no_plan_skipped(tmp_path, capsys):
    """No PLAN.md -- gate skips."""
    rc = mutation_sample.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_explicit_false_skipped(tmp_path, capsys):
    """mutation_testing: false is the same as not declared -- skipped."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(mutation_testing=False))
    rc = mutation_sample.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_opt_in_emits_not_yet_implemented(tmp_path, capsys):
    """mutation_testing: true -- WARN with `not yet implemented`."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(mutation_testing=True))
    rc = mutation_sample.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "not yet implemented" in out.lower()
