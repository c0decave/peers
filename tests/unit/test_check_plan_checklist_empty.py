"""Test plan-checklist-empty check (Task 2.1)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import plan_checklist_empty


def _write_plan(tmp_path: Path, steps_lines: list[str]) -> Path:
    """Write a minimal valid PLAN.md to tmp_path with the given step lines."""
    plan = tmp_path / "PLAN.md"
    body = "\n".join(steps_lines)
    plan.write_text(f"""# Feature
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{body}
""")
    return plan


def test_empty_checklist_passes(tmp_path, capsys):
    _write_plan(tmp_path, [
        "- [x] [STEP-1] done",
        "  - touches: src/a.py",
        "- [x] [STEP-2] also done",
        "  - touches: src/b.py",
    ])
    rc = plan_checklist_empty.main(str(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "clean" in out


def test_one_open_item_fails(tmp_path, capsys):
    _write_plan(tmp_path, [
        "- [x] [STEP-1] done",
        "  - touches: src/a.py",
        "- [ ] [STEP-2] open",
        "  - touches: src/b.py",
        "- [x] [STEP-3] done",
        "  - touches: src/c.py",
    ])
    rc = plan_checklist_empty.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out
    assert "STEP-1" not in out
    assert "STEP-3" not in out


def test_all_open_fails(tmp_path, capsys):
    _write_plan(tmp_path, [
        "- [ ] [STEP-1] a",
        "  - touches: src/a.py",
        "- [ ] [STEP-2] b",
        "  - touches: src/b.py",
    ])
    rc = plan_checklist_empty.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "STEP-2" in out


def test_missing_plan_md_fails(tmp_path, capsys):
    rc = plan_checklist_empty.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "PLAN.md not found" in out


def test_invalid_plan_md_fails(tmp_path, capsys):
    plan = tmp_path / "PLAN.md"
    plan.write_text("# Header\n## Not Meta or Steps\nrandom content\n")
    rc = plan_checklist_empty.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "invalid" in out.lower() or "PLAN.md" in out
