#!/usr/bin/env python3
"""Exit 1 if any checked PLAN.md step lacks a real git commit it can be traced to.

Third hard gate for implement-mode. PLAN.md is the contract of the run;
checking a step off claims the work is done. This gate enforces that the
claim is grounded in git history rather than vibes:

For every step in state ``done`` (i.e. ``- [x]``) the gate requires:

1. A trailing ``(SHA)`` annotation on the step line, e.g.
   ``- [x] [STEP-3] add auth (a7f96c3)`` (7..40 hex chars).
2. The SHA must resolve to a real commit in the local git history
   (``git rev-parse --verify <sha>^{commit}``).
3. If the step declares ``- touches: <files>``, the commit's changed
   file set must intersect that list. (Empty intersection = peer
   annotated the wrong commit, or the commit didn't do the work
   the step described.)

Pass (exit 0) when every checked step satisfies 1-3.
Fail (exit 1) with a per-step diagnostic when any of the above is
violated. Unchecked steps are skip-friendly: they need no annotation.
Missing PLAN.md or schema-invalid PLAN.md is also a hard failure.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


def _commit_exists(project_root: Path, sha: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _commit_files(project_root: Path, sha: str) -> list[str]:
    """Return the list of file paths changed by commit ``sha``.

    Uses ``git show --name-only --format=`` so the output is purely a
    newline-separated list of paths (no commit header).
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "show", "--name-only", "--format=", sha],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main(project_dir: str = ".") -> int:
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("plan-step-traceable FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"plan-step-traceable FAIL: PLAN.md invalid: {e}")
        return 1

    checked = [s for s in plan.steps if s.state == "done"]
    if not checked:
        print("plan-step-traceable: clean (no checked steps to verify)")
        return 0

    violations: list[str] = []
    for step in checked:
        if not step.commit_sha:
            violations.append(
                f"  {step.id}: missing trailing (SHA) annotation — "
                f"checked step has no commit reference"
            )
            continue
        if not _commit_exists(project_root, step.commit_sha):
            violations.append(
                f"  {step.id}: SHA {step.commit_sha} not found in git history "
                f"(unknown commit)"
            )
            continue
        if step.touches:
            changed = set(_commit_files(project_root, step.commit_sha))
            declared = set(step.touches)
            if not (changed & declared):
                violations.append(
                    f"  {step.id}: commit {step.commit_sha} touches "
                    f"{sorted(changed) or '[]'} but declared touches: "
                    f"{sorted(declared)} (no overlap)"
                )

    if violations:
        print(
            f"plan-step-traceable FAIL: "
            f"{len(violations)}/{len(checked)} checked step(s) untraceable:"
        )
        for v in violations:
            print(v)
        return 1

    print(
        f"plan-step-traceable: clean ({len(checked)} checked step(s), "
        f"all traced to real commits)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
