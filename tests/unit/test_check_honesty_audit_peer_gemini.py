"""Test honesty-audit-peer-gemini opt-in soft gate (Task 8.4).

Opt-in soft gate -- when PLAN.md Meta declares
`honesty_audit_peer: <name>`, verifies HONESTY_AUDIT.md has a matching
`## <name>` H2 section. Skipped (exit 0) when no opt-in declared.
Always exits 0.
"""
from __future__ import annotations

from peers.templates.modes.implement.checks import honesty_audit_peer_gemini


def _meta_plan(honesty_audit_peer: str | None = None) -> str:
    """Build a PLAN with a Meta section."""
    lines = ["# F\n", "\n", "## Meta\n", "surfaces: [cli]\n", "acceptance: pytest\n"]
    if honesty_audit_peer is not None:
        lines.append(f"honesty_audit_peer: {honesty_audit_peer}\n")
    lines.append("\n")
    lines.append("## Steps\n")
    lines.append("- [ ] [STEP-1] do thing\n")
    return "".join(lines)


def test_no_opt_in_skipped(tmp_path, capsys):
    """No honesty_audit_peer set -- gate skips with exit 0."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(honesty_audit_peer=None))
    rc = honesty_audit_peer_gemini.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_no_plan_at_all_skipped(tmp_path, capsys):
    """No PLAN.md at all -- gate skips (nothing to opt in to)."""
    rc = honesty_audit_peer_gemini.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_opt_in_with_matching_section_passes(tmp_path, capsys):
    """Opt-in declared AND HONESTY_AUDIT.md has ## gemini section -- clean."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(honesty_audit_peer="gemini"))
    audit = (
        "# Honesty Audit\n\n"
        "## claude\n### Weakest part\nbody\n"
        "## codex\n### Weakest part\nbody\n"
        "## gemini\n### Weakest part\nbody\n"
    )
    (tmp_path / "HONESTY_AUDIT.md").write_text(audit)
    rc = honesty_audit_peer_gemini.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_opt_in_without_section_warns(tmp_path, capsys):
    """Opt-in declared but no matching section -- soft warn (still exit 0)."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(honesty_audit_peer="gemini"))
    audit = (
        "# Honesty Audit\n\n"
        "## claude\n### Weakest part\nbody\n"
        "## codex\n### Weakest part\nbody\n"
    )
    (tmp_path / "HONESTY_AUDIT.md").write_text(audit)
    rc = honesty_audit_peer_gemini.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
    assert "gemini" in out.lower()


def test_opt_in_without_audit_file_warns(tmp_path, capsys):
    """Opt-in declared but HONESTY_AUDIT.md missing entirely -- soft warn."""
    (tmp_path / "PLAN.md").write_text(_meta_plan(honesty_audit_peer="gemini"))
    rc = honesty_audit_peer_gemini.main(str(tmp_path))
    assert rc == 0  # soft
    out = capsys.readouterr().out
    assert "warn" in out.lower()
