"""Test plan-original-preserved check (Task 2.5)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import plan_original_preserved


def _with_touches(steps: list[str]) -> list[str]:
    """Append a synthetic `touches:` sub-line to each `[ ] [STEP-N] ...`
    entry so the steps satisfy the parser's I4 validation. Steps that
    already declare touches (or any sub-key starting with two spaces)
    are left untouched."""
    out: list[str] = []
    for line in steps:
        out.append(line)
        # Extract STEP-N identifier from `- [marker] [STEP-N] ...`.
        import re as _re
        m = _re.search(r"\[(STEP-\d+)\]", line)
        if m:
            out.append(f"  - touches: src/{m.group(1).lower()}.py")
    return out


def _setup(tmp_path: Path, original_steps: list[str], current_steps: list[str]) -> Path:
    """Create project dir with PLAN.md (current) + .peers/PLAN.original.md (frozen)."""
    plan = tmp_path / "PLAN.md"
    plan.write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{chr(10).join(_with_touches(current_steps))}
""")
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    plan_orig = plan_dir / "PLAN.original.md"
    plan_orig.write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{chr(10).join(_with_touches(original_steps))}
""")
    return tmp_path


def test_all_original_preserved_passes(tmp_path, capsys):
    _setup(tmp_path,
        original_steps=["- [ ] [STEP-1] a", "- [ ] [STEP-2] b"],
        current_steps=["- [x] [STEP-1] a", "- [ ] [STEP-2] b"],
    )
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 0


def test_adding_new_steps_passes(tmp_path, capsys):
    _setup(tmp_path,
        original_steps=["- [ ] [STEP-1] a", "- [ ] [STEP-2] b"],
        current_steps=["- [x] [STEP-1] a", "- [ ] [STEP-2] b", "- [ ] [STEP-3] new"],
    )
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 0


def test_missing_original_step_fails(tmp_path, capsys):
    _setup(tmp_path,
        original_steps=["- [ ] [STEP-1] a", "- [ ] [STEP-2] b", "- [ ] [STEP-3] c"],
        current_steps=["- [x] [STEP-1] a", "- [ ] [STEP-3] c"],  # STEP-2 dropped
    )
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out


def test_missing_plan_md_fails(tmp_path, capsys):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    (plan_dir / "PLAN.original.md").write_text("# F\n## Meta\nsurfaces: [cli]\nacceptance: x\n## Steps\n- [ ] [STEP-1] x\n")
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.md" in out and "not found" in out


def test_missing_plan_original_md_fails(tmp_path, capsys):
    (tmp_path / "PLAN.md").write_text("# F\n## Meta\nsurfaces: [cli]\nacceptance: x\n## Steps\n- [ ] [STEP-1] x\n")
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.original.md" in out


def test_invalid_current_plan_md_fails(tmp_path, capsys):
    plan_dir = tmp_path / ".peers"
    plan_dir.mkdir()
    (plan_dir / "PLAN.original.md").write_text("# F\n## Meta\nsurfaces: [cli]\nacceptance: x\n## Steps\n- [ ] [STEP-1] x\n")
    (tmp_path / "PLAN.md").write_text("# Broken\n## not meta\nrandom\n")
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "invalid" in out.lower() or "PLAN.md" in out


def test_symlinked_current_plan_fails_closed_BUG_255(tmp_path, capsys):
    _setup(
        tmp_path,
        original_steps=["- [ ] [STEP-1] a"],
        current_steps=["- [ ] [STEP-1] a"],
    )
    outside = tmp_path / "outside-plan.md"
    outside.write_text((tmp_path / "PLAN.md").read_text(), encoding="utf-8")
    (tmp_path / "PLAN.md").unlink()
    (tmp_path / "PLAN.md").symlink_to(outside)

    rc = plan_original_preserved.main(str(tmp_path))

    assert rc == 1
    out = capsys.readouterr().out.lower()
    assert "plan.md" in out
    assert "symlink" in out or "symbolic" in out or "unsafe" in out


def test_multiple_missing_listed(tmp_path, capsys):
    _setup(tmp_path,
        original_steps=["- [ ] [STEP-1] a", "- [ ] [STEP-2] b", "- [ ] [STEP-3] c", "- [ ] [STEP-4] d"],
        current_steps=["- [ ] [STEP-1] a", "- [ ] [STEP-4] d"],  # STEP-2, STEP-3 dropped
    )
    rc = plan_original_preserved.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out
    assert "STEP-3" in out
