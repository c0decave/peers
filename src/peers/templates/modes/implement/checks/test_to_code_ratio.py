#!/usr/bin/env python3
"""Soft cleanliness gate: warn when a step commit's test-to-code ratio is low.

Schicht-5 soft gate for implement-mode (Task 5.5.4). For every PLAN.md
step in state ``done`` with a ``(SHA)`` annotation, count substantive
lines added under ``tests/`` versus those added under ``src/`` in that
commit. Warn if ``tests_loc < _MIN_RATIO * src_loc``.

A commit that adds tests but no src LOC is treated as well-covered
(ratio undefined -> not flagged).

Exemption: steps carrying ``pure_refactor: true`` are not warned --
restructuring without behaviour change shouldn't need new tests.

Soft semantics: always exit 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


_MIN_RATIO = 0.5

_DIFF_ADDED_RE = re.compile(r"^\+(?!\+\+)(?P<body>.*)$")


def _added_substantive_loc(project_root: Path, sha: str, subdir: str) -> int:
    """Count substantive (non-blank, non-comment) added LOC under subdir."""
    proc = subprocess.run(
        [
            "git", "-C", str(project_root),
            "show", "--format=", "--diff-filter=AM", sha, "--", f"{subdir}/",
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
    """Soft per-step ratio: warn on commits with tests_loc < 0.5 * src_loc."""
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("test-to-code-ratio: clean (no PLAN.md)")
        return 0
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"test-to-code-ratio: skipped (PLAN.md invalid: {e})")
        return 0
    checked = [s for s in plan.steps if s.state == "done"]
    if not checked:
        print("test-to-code-ratio: clean (no checked steps)")
        return 0
    findings: list[str] = []
    verified = 0
    exempt = 0
    deferred = 0
    for step in checked:
        if not step.commit_sha:
            deferred += 1
            continue
        if step.pure_refactor:
            exempt += 1
            continue
        src_loc = _added_substantive_loc(project_root, step.commit_sha, "src")
        if src_loc == 0:
            # No src LOC added -> ratio undefined, treat as fine.
            verified += 1
            continue
        test_loc = _added_substantive_loc(
            project_root, step.commit_sha, "tests"
        )
        if test_loc < _MIN_RATIO * src_loc:
            findings.append(
                f"{step.id}: commit {step.commit_sha} test_loc={test_loc} "
                f"src_loc={src_loc} ratio="
                f"{test_loc / src_loc:.2f} (< {_MIN_RATIO})"
            )
        else:
            verified += 1
    if findings:
        print(
            f"test-to-code-ratio WARN: {len(findings)} step(s) "
            f"under-tested:"
        )
        for f in findings:
            print(f"  {f}")
        print(
            "  hint: add tests for the new src code, or if the step "
            "is a pure refactor, add `- pure_refactor: true` under the "
            "PLAN.md step"
        )
        return 0  # soft
    print(
        f"test-to-code-ratio: clean "
        f"({verified} sized, {exempt} pure-refactor, {deferred} deferred)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
