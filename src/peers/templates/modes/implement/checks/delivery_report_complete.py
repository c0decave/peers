#!/usr/bin/env python3
"""Exit 1 unless DELIVERY.md maps every PLAN.md step to a structured entry.

Schicht-3 convergence anchor for implement-mode (Task 3.3). At
convergence the peers must produce a ``DELIVERY.md`` that maps every
original STEP-N from PLAN.md to a structured entry capturing what
landed, how it was tested, and why. This gate verifies that report
exists and is structurally complete; it does NOT re-prove the work
(``plan-step-traceable`` and ``coverage-3class-delta`` already do
that).

Required per-step structure
---------------------------

    ## [STEP-N] <free text>
    - **Commit:** <sha or PENDING or BLOCKED>
    - **Tests:** <test-files or N/A>
    - **Justification:** <prose, must be non-empty>

The three field labels (Commit, Tests, Justification) are matched
case-sensitively to keep the contract crisp; the values are free-form.
``PENDING`` and ``BLOCKED`` are explicitly allowed in the Commit field
as escape valves from Schicht 4 (an in-flight step waiting on external
input must still appear in the report). SHA validity is intentionally
not enforced here — that is plan-step-traceable's job.

Pass (exit 0) when:
    * DELIVERY.md exists
    * Every STEP-N in PLAN.md (regardless of [ ]/[x] state) has a
      ``## [STEP-N] ...`` section in DELIVERY.md
    * Each section has Commit + Tests + Justification fields
    * Each Justification is non-empty (whitespace-only fails)

Fail (exit 1) with a per-violation diagnostic otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


# Match a DELIVERY.md step heading, e.g. "## [STEP-3] add auth"
_HEADING_RE = re.compile(r"^##\s*\[(?P<id>STEP-\d+)\]\s*(?P<text>.*?)\s*$")

# Match a field bullet, e.g. "- **Commit:** abc1234"
_FIELD_RE = re.compile(
    r"^\s*-\s*\*\*(?P<key>Commit|Tests|Justification):\*\*\s*(?P<val>.*?)\s*$"
)

_REQUIRED_FIELDS = ("Commit", "Tests", "Justification")


def _parse_delivery(text: str) -> dict[str, dict[str, str]]:
    """Return ``{step_id: {field_key: value}}`` from DELIVERY.md text.

    Sections are delimited by the next ``## [STEP-N]`` heading (or EOF).
    Field values are captured raw; whitespace-only values survive so the
    Justification non-empty check can flag them.
    """
    sections: dict[str, dict[str, str]] = {}
    current_id: str | None = None
    for line in text.splitlines():
        m_head = _HEADING_RE.match(line)
        if m_head:
            current_id = m_head.group("id")
            sections.setdefault(current_id, {})
            continue
        if current_id is None:
            continue
        m_field = _FIELD_RE.match(line)
        if m_field:
            key = m_field.group("key")
            # First occurrence wins; subsequent dupes are ignored so a
            # peer can't paper over an empty field by adding a second
            # populated one below it.
            sections[current_id].setdefault(key, m_field.group("val"))
    return sections


def main(project_dir: str = ".") -> int:
    """Verify DELIVERY.md maps every PLAN.md step with a structured entry."""
    project_root = Path(project_dir).resolve()

    delivery_path = project_root / "DELIVERY.md"
    if not delivery_path.is_file():
        print("delivery-report-complete FAIL: DELIVERY.md not found")
        return 1

    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("delivery-report-complete FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"delivery-report-complete FAIL: PLAN.md invalid: {e}")
        return 1

    expected_ids = [s.id for s in plan.steps]
    sections = _parse_delivery(delivery_path.read_text())

    violations: list[str] = []
    for step_id in expected_ids:
        if step_id not in sections:
            violations.append(f"{step_id} absent")
            continue
        fields = sections[step_id]
        missing = [f for f in _REQUIRED_FIELDS if f not in fields]
        for label in missing:
            violations.append(f"{step_id} missing '{label}' field")
        if "Justification" in fields and not fields["Justification"].strip():
            violations.append(f"{step_id} 'Justification' is empty")

    if violations:
        print(
            f"delivery-report-complete FAIL: "
            f"{len(violations)} violation(s): " + "; ".join(violations)
        )
        return 1

    print(
        f"delivery-report-complete: all {len(expected_ids)} steps mapped"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
