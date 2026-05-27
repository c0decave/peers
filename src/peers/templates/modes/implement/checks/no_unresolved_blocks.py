#!/usr/bin/env python3
"""Exit 1 if PLAN.md contains any `[BLOCKED]` step (Task 7.2).

Schicht-4 escape-valve gate. The implement-mode parser recognises five
step-state markers (Task 7.1):

    [ ]            -> open
    [x] / [X]      -> done
    [PARTIAL]      -> partial
    [BLOCKED]      -> blocked
    [BLOCKED-ACK]  -> blocked-ack

A `[BLOCKED]` step means the loop hit something it cannot finish on its
own (missing API key, an external service is down, an upstream dep is
not yet released). That is a legitimate state DURING a run -- the gate
only fires at convergence. At convergence every blocked step must be
either:

  - resolved (state flipped to `done`), or
  - explicitly acknowledged by the operator via `peers-ctl ack-block`
    (Task 7.3), which rewrites the marker from `[BLOCKED]` to
    `[BLOCKED-ACK]`.

A bare `[BLOCKED]` at convergence means no human ever looked at it; the
loop must not be allowed to declare success in that state.

Pass (exit 0) when no step is in `blocked` state. `partial` and `open`
steps don't fail this gate (they are caught by `plan-checklist-empty`);
`blocked-ack` is treated as resolved -- the operator has signed off.

Fail (exit 1) with stdout listing the offending step IDs when any step
is in `blocked` state.

Fail (exit 1) with stdout `PLAN.md not found` if missing, or
`PLAN.md invalid: ...` on parse error -- same shape as the other
implement-mode gates so the operator sees a consistent failure surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


def main(project_dir: str = ".") -> int:
    plan_path = Path(project_dir) / "PLAN.md"
    if not plan_path.is_file():
        print("no-unresolved-blocks FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"no-unresolved-blocks FAIL: PLAN.md invalid: {e}")
        return 1
    blocked_ids = [s.id for s in plan.steps if s.state == "blocked"]
    if blocked_ids:
        print(
            "no-unresolved-blocks FAIL: blocked steps (need operator "
            "ack via `peers-ctl ack-block`): "
            + ", ".join(blocked_ids)
        )
        return 1
    print(
        f"no-unresolved-blocks: clean ({len(plan.steps)} steps, "
        "no unacknowledged blocks)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
