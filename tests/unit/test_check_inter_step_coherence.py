"""Test inter-step-coherence soft gate (Task 8.3).

Soft gate -- verifies STITCH.md tracks inter-step coherence checks.
Every N=3 completed steps trigger a stitch check; reviewer writes
STITCH.md explaining how recent steps fit together (duplications,
dangling connections). Always exits 0.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import inter_step_coherence


_PLAN_HEADER = (
    "# PLAN\n"
    "\n"
    "## Surfaces\n"
    "\n"
    "(no UI)\n"
    "\n"
    "## Acceptance\n"
    "\n"
    "All gates green.\n"
    "\n"
    "## Steps\n"
    "\n"
)


def _plan(steps: list[tuple[str, str, str]]) -> str:
    """Build a PLAN body. Each step: (id, marker, summary)."""
    lines = [_PLAN_HEADER]
    for sid, mark, summary in steps:
        lines.append(f"- [{mark}] {sid}: {summary}\n")
    return "".join(lines)


def test_missing_file_with_few_steps_passes(tmp_path, capsys):
    """No STITCH.md and <3 checked steps -- nothing to stitch yet."""
    plan = _plan([
        ("STEP-1", "x", "wire up parser (abc123)"),
        ("STEP-2", " ", "add tests"),
    ])
    (tmp_path / "PLAN.md").write_text(plan)
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_missing_file_with_many_steps_warns(tmp_path, capsys):
    """>3 checked steps but no STITCH.md -- soft warn."""
    plan = _plan([
        ("STEP-1", "x", "do A (abc1234)"),
        ("STEP-2", "x", "do B (abc1235)"),
        ("STEP-3", "x", "do C (abc1236)"),
        ("STEP-4", "x", "do D (abc1237)"),
    ])
    (tmp_path / "PLAN.md").write_text(plan)
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "STITCH" in out or "stitch" in out.lower()


def test_substantive_entries_pass(tmp_path, capsys):
    """STITCH.md entries with substantive prose (>20 words each) pass."""
    plan = _plan([
        ("STEP-1", "x", "do A (abc1234)"),
        ("STEP-2", "x", "do B (abc1235)"),
        ("STEP-3", "x", "do C (abc1236)"),
        ("STEP-4", "x", "do D (abc1237)"),
    ])
    (tmp_path / "PLAN.md").write_text(plan)
    stitch = (
        "# Stitch Log\n"
        "\n"
        "## Stitch 1 -- STEP-1..STEP-3\n"
        "\n"
        "The three steps wire the parser into the orchestrator and add the\n"
        "first coverage layer. There is one dangling connection: STEP-2\n"
        "renames an API used by STEP-1 -- still consistent because both\n"
        "land in the same commit chain and tests cover the new shape.\n"
    )
    (tmp_path / "STITCH.md").write_text(stitch)
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_thin_entries_warn(tmp_path, capsys):
    """STITCH.md entries with thin prose (<20 words) -- soft warn."""
    plan = _plan([
        ("STEP-1", "x", "do A (abc1234)"),
        ("STEP-2", "x", "do B (abc1235)"),
        ("STEP-3", "x", "do C (abc1236)"),
        ("STEP-4", "x", "do D (abc1237)"),
    ])
    (tmp_path / "PLAN.md").write_text(plan)
    stitch = (
        "# Stitch Log\n"
        "\n"
        "## Stitch 1 -- STEP-1..STEP-3\n"
        "\n"
        "Looks fine.\n"
    )
    (tmp_path / "STITCH.md").write_text(stitch)
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()


def test_missing_plan_passes(tmp_path, capsys):
    """No PLAN.md -- nothing to stitch."""
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out or "no PLAN" in out


def test_exactly_three_steps_no_stitch_passes(tmp_path, capsys):
    """Boundary: exactly 3 checked steps -- gate only fires at >3."""
    plan = _plan([
        ("STEP-1", "x", "do A (abc1234)"),
        ("STEP-2", "x", "do B (abc1235)"),
        ("STEP-3", "x", "do C (abc1236)"),
    ])
    (tmp_path / "PLAN.md").write_text(plan)
    rc = inter_step_coherence.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out
