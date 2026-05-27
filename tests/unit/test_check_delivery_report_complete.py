"""Test delivery-report-complete check (Task 3.3)."""
from __future__ import annotations
from pathlib import Path

from peers.templates.modes.implement.checks import delivery_report_complete


def _make_plan(tmp_path: Path, step_count: int = 2):
    plan = tmp_path / "PLAN.md"
    steps = "\n".join(
        f"- [ ] [STEP-{i}] step {i}\n  - touches: src/step{i}.py"
        for i in range(1, step_count + 1)
    )
    plan.write_text(f"""# F
## Meta
surfaces: [cli]
acceptance: pytest
## Steps
{steps}
""")


def _make_delivery(tmp_path: Path, content: str):
    (tmp_path / "DELIVERY.md").write_text(content)


def test_complete_delivery_passes(tmp_path, capsys):
    _make_plan(tmp_path, 2)
    _make_delivery(tmp_path, """# Delivery

## [STEP-1] step 1
- **Commit:** abc1234
- **Tests:** tests/test_step1.py
- **Justification:** Implemented step 1 as specified, with 3 test cases.

## [STEP-2] step 2
- **Commit:** def5678
- **Tests:** tests/test_step2.py
- **Justification:** Implemented step 2 with edge case coverage.
""")
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 0


def test_missing_delivery_fails(tmp_path, capsys):
    _make_plan(tmp_path)
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DELIVERY.md" in out and "not found" in out


def test_step_missing_from_delivery_fails(tmp_path, capsys):
    _make_plan(tmp_path, 3)
    _make_delivery(tmp_path, """# Delivery

## [STEP-1] step 1
- **Commit:** abc1234
- **Tests:** tests/
- **Justification:** Done.

## [STEP-3] step 3
- **Commit:** ghi9999
- **Tests:** tests/
- **Justification:** Done.
""")  # STEP-2 missing
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-2" in out


def test_section_missing_commit_field_fails(tmp_path, capsys):
    _make_plan(tmp_path, 1)
    _make_delivery(tmp_path, """# Delivery

## [STEP-1] step 1
- **Tests:** tests/
- **Justification:** Done.
""")
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "STEP-1" in out
    assert "Commit" in out


def test_section_missing_tests_field_fails(tmp_path, capsys):
    _make_plan(tmp_path, 1)
    _make_delivery(tmp_path, """# Delivery

## [STEP-1] step 1
- **Commit:** abc1234
- **Justification:** Done.
""")
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Tests" in out


def test_empty_justification_fails(tmp_path, capsys):
    _make_plan(tmp_path, 1)
    # Note: trailing whitespace after "Justification:" is intentional;
    # any-whitespace-only justification must be rejected.
    _make_delivery(
        tmp_path,
        "# Delivery\n\n"
        "## [STEP-1] step 1\n"
        "- **Commit:** abc1234\n"
        "- **Tests:** tests/\n"
        "- **Justification:**   \n"
        "\n",
    )
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 1
    out = capsys.readouterr().out
    assert "Justification" in out


def test_pending_commit_allowed(tmp_path, capsys):
    _make_plan(tmp_path, 1)
    _make_delivery(tmp_path, """# Delivery

## [STEP-1] step 1
- **Commit:** PENDING
- **Tests:** N/A
- **Justification:** Blocked on external API access.
""")
    rc = delivery_report_complete.main(str(tmp_path))
    assert rc == 0
