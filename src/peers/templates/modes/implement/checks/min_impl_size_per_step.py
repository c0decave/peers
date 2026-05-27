#!/usr/bin/env python3
"""Soft cleanliness gate: warn when a step commit is too small.

Schicht-5 soft gate for implement-mode (Task 5.5.3). For every PLAN.md
step in state ``done`` with a ``(SHA)`` annotation, count substantive
lines added under ``src/`` in that commit (excluding blank and
comment-only lines). Warn if the count is below ``_MIN_SUBSTANTIVE_LOC``.

Exemption: steps carrying ``trivial_step: true`` (or the short form
``trivial: true``) are not warned -- the implementer pre-declared the
step as small.

Soft semantics: always exit 0. Findings are advisory; the reviewer
peer reads stdout via the companion ``soft`` goal.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


_MIN_SUBSTANTIVE_LOC = 10

# Match a `+...` line in a unified diff that isn't a file header (`+++`).
# We strip the leading `+` and trim trailing whitespace before deciding
# whether the line is substantive.
_DIFF_ADDED_RE = re.compile(r"^\+(?!\+\+)(?P<body>.*)$")


def _added_substantive_src_loc(project_root: Path, sha: str) -> int:
    """Count diff-added lines under src/ that aren't whitespace or pure comments."""
    proc = subprocess.run(
        [
            "git", "-C", str(project_root),
            "show", "--format=", "--diff-filter=AM", sha,
            "--", "src/",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return 0
    count = 0
    for line in proc.stdout.splitlines():
        m = _DIFF_ADDED_RE.match(line)
        if not m:
            continue
        body = m.group("body").rstrip()
        stripped = body.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        count += 1
    return count


def main(project_dir: str = ".") -> int:
    """Soft per-step sizer: warn on commits adding <10 substantive src LOC."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("min-impl-size-per-step: clean (no PLAN.md)")
        return 0
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"min-impl-size-per-step: skipped (PLAN.md invalid: {e})")
        return 0  # soft -- don't punish PLAN errors here
    checked = [s for s in plan.steps if s.state == "done"]
    if not checked:
        print("min-impl-size-per-step: clean (no checked steps)")
        return 0
    findings: list[str] = []
    verified = 0
    exempt = 0
    deferred = 0
    for step in checked:
        if not step.commit_sha:
            deferred += 1
            continue
        if step.trivial:
            exempt += 1
            continue
        loc = _added_substantive_src_loc(project_root, step.commit_sha)
        if loc < _MIN_SUBSTANTIVE_LOC:
            findings.append(
                f"{step.id}: commit {step.commit_sha} added {loc} "
                f"substantive src LOC (< {_MIN_SUBSTANTIVE_LOC})"
            )
        else:
            verified += 1
    if findings:
        print(
            f"min-impl-size-per-step WARN: {len(findings)} step(s) "
            f"with under-sized commits:"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: if the step is intentionally small, add "
            "`- trivial_step: true` under the PLAN.md step"
        )
        return 0  # soft
    print(
        f"min-impl-size-per-step: clean "
        f"({verified} sized, {exempt} trivial, {deferred} deferred)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
