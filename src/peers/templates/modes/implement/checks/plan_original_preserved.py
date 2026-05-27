#!/usr/bin/env python3
"""Exit 1 if any original PLAN.md step ID has been dropped from current PLAN.md.

Fourth hard gate for implement-mode. PLAN.md is the contract; the frozen
copy at ``<project>/.peers/PLAN.original.md`` is the contract as accepted
at project init. This gate enforces that the live PLAN.md still contains
every step ID from the frozen original — i.e. nobody silently dropped a
step out of scope between init and the convergence claim.

Pass (exit 0) when every STEP-N from PLAN.original.md still appears in
the current PLAN.md. Additions (new STEP-N+1, STEP-N+2, ...) are fine —
only removals/renames are rejected.

Fail (exit 1) with stdout listing the missing original IDs (sorted).
Missing PLAN.md, missing PLAN.original.md, or empty-on-the-ID-axis
PLAN.md are all hard failures with diagnostics.

Note: the current PLAN.md is scanned for ``[STEP-N]`` IDs via regex
rather than full ``parse_plan``. The reason is by design: when a peer
silently drops STEP-2 and STEP-3 from a [1, 2, 3, 4] plan, the
remainder ([1, 4]) is no longer sequential and ``parse_plan`` would
reject it before we could compute the dropped-IDs diagnostic. Regex
extraction lets us name the missing IDs even in that case. The frozen
original is read via ``parse_plan`` because it was validated at freeze.

Only step IDs are compared; per-step fields (touches, deps, body) are out
of scope here — this is purely the "no silent scope-drop" gate.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan

_STEP_ID_RE = re.compile(r"\[(STEP-\d+)\]")


def _extract_step_ids(text: str) -> set[str]:
    """Return the set of ``STEP-N`` IDs appearing in ``text``."""
    return set(_STEP_ID_RE.findall(text))


def main(project_dir: str = ".") -> int:
    project_root = Path(project_dir)
    plan_path = project_root / "PLAN.md"
    original_path = project_root / ".peers" / "PLAN.original.md"

    if not original_path.is_file():
        print("plan-original-preserved FAIL: PLAN.original.md not found")
        return 1
    if not plan_path.is_file():
        print("plan-original-preserved FAIL: PLAN.md not found")
        return 1

    try:
        original_plan = parse_plan(original_path)
    except PlanValidationError as e:
        print(f"plan-original-preserved FAIL: PLAN.original.md invalid: {e}")
        return 1

    original_ids = {step.id for step in original_plan.steps}
    current_ids = _extract_step_ids(plan_path.read_text())

    missing = original_ids - current_ids
    if missing:
        missing_sorted = sorted(missing)
        print(
            "plan-original-preserved FAIL: "
            f"{len(missing_sorted)} original step(s) dropped from PLAN.md: "
            + ", ".join(missing_sorted)
        )
        return 1

    print(
        f"plan-original-preserved: clean ({len(original_ids)} original step(s) "
        f"all present in current PLAN.md)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
