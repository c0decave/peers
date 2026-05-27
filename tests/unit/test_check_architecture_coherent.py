"""Test architecture-coherent soft gate (Task 8.2).

Soft gate -- compares ARCHITECTURE.actual.md (current, generated at
convergence) against ARCHITECTURE.intended.md (frozen at Phase 0).
Significant structural divergence without documented amendment in
PLAN.md is flagged. Always exits 0.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import architecture_coherent


_INTENDED = """\
# Architecture (intended)

## Components

- foo: does X
- bar: does Y
- baz: does Z

## Data Flow

Input -> foo -> bar -> baz -> Output

## Dependencies

- requests
- pyyaml
"""


def test_both_files_aligned_pass(tmp_path, capsys):
    """Same headings + small diff -> clean."""
    (tmp_path / "ARCHITECTURE.intended.md").write_text(_INTENDED)
    (tmp_path / "ARCHITECTURE.actual.md").write_text(_INTENDED)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_intended_missing_warns(tmp_path, capsys):
    """Phase 0 didn't write ARCHITECTURE.intended.md -- soft warn."""
    (tmp_path / "ARCHITECTURE.actual.md").write_text(_INTENDED)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "intended" in out.lower()


def test_actual_missing_warns(tmp_path, capsys):
    """Convergence didn't write ARCHITECTURE.actual.md -- soft warn."""
    (tmp_path / "ARCHITECTURE.intended.md").write_text(_INTENDED)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()
    assert "actual" in out.lower()


def test_structural_divergence_warns(tmp_path, capsys):
    """Headings differ significantly -- flagged unless PLAN.md documents."""
    (tmp_path / "ARCHITECTURE.intended.md").write_text(_INTENDED)
    actual = (
        "# Architecture (actual)\n"
        "\n"
        "## Something Different\n"
        "\n"
        "Random prose without the expected sections.\n"
    )
    (tmp_path / "ARCHITECTURE.actual.md").write_text(actual)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "WARN" in out or "warn" in out.lower()


def test_divergence_with_amendment_passes(tmp_path, capsys):
    """Divergence is OK if PLAN.md carries an architecture amendment."""
    (tmp_path / "ARCHITECTURE.intended.md").write_text(_INTENDED)
    actual = (
        "# Architecture (actual)\n"
        "\n"
        "## Something Different\n"
        "\n"
        "Random prose without the expected sections.\n"
    )
    (tmp_path / "ARCHITECTURE.actual.md").write_text(actual)
    plan = (
        "# PLAN\n"
        "\n"
        "## Architecture Amendment\n"
        "\n"
        "The intended architecture from Phase 0 was revised to drop the\n"
        "Data Flow section because the design shifted to event-driven.\n"
    )
    (tmp_path / "PLAN.md").write_text(plan)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out or "amendment" in out.lower()


def test_small_text_diff_passes(tmp_path, capsys):
    """Headings identical, body slightly different -> clean."""
    (tmp_path / "ARCHITECTURE.intended.md").write_text(_INTENDED)
    actual = _INTENDED.replace("does X", "does X1")
    (tmp_path / "ARCHITECTURE.actual.md").write_text(actual)
    rc = architecture_coherent.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out
