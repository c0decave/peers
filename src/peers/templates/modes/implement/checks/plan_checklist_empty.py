#!/usr/bin/env python3
"""Exit 1 if PLAN.md still has unchecked `- [ ]` step items.

First hard gate for implement-mode. The check is the structural counterpart
to PLAN.md being the contract for the run: if any step is still open, the
implementation is by definition incomplete and the convergence loop should
not be allowed to claim success.

Pass (exit 0) when all `- [ ]` are checked off.
Fail (exit 1) with stdout listing open step IDs when any remain.
Fail (exit 1) with stdout 'PLAN.md not found' if missing.
Fail (exit 1) with stdout 'PLAN.md invalid: ...' on parse error.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


def main(project_dir: str = ".") -> int:
    plan_path = Path(project_dir) / "PLAN.md"
    if not plan_path.is_file():
        print("plan-checklist-empty FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"plan-checklist-empty FAIL: PLAN.md invalid: {e}")
        return 1
    open_ids = [step.id for step in plan.steps if step.state == "open"]
    if open_ids:
        print(
            "plan-checklist-empty FAIL: open steps: "
            + ", ".join(open_ids)
        )
        return 1
    print(
        f"plan-checklist-empty: clean ({len(plan.steps)} steps, all checked)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
