#!/usr/bin/env python3
"""Exit 1 if any checked PLAN.md step's commit lacks happy+edge+sad tests.

Fifth hard gate for implement-mode. This is the per-step delta version
of audit-mode's whole-tree ``coverage_3class`` check: it does not look
at the project's tests as a whole, it looks at *what each completed
step actually added*.

For every step in state ``done`` (``- [x]``) that has a trailing
``(SHA)`` annotation, this gate:

1. Extracts the test functions added by that commit
   (``git show --diff-filter=AM SHA -- tests/``, then scan added
   ``+def test_*`` lines from the diff).
2. Classifies each name into ``happy`` / ``edge`` / ``sad`` using the
   same keyword vocabulary as ``audit/checks/coverage_3class`` —
   imported directly (``KIND_RE``) so classifications stay in sync.
3. Passes if all three classes have at least one test.
4. Fails with a per-step diagnostic naming the missing class(es), or
   "no new test functions in commit" if the commit added zero
   ``def test_*`` lines under ``tests/``.

Steps without ``commit_sha`` are *not* failures here — that's
``plan-step-traceable``'s job. We log them as "not verifiable" so
the operator can see they were considered but deferred.

Pass (exit 0) when:
- PLAN.md has no ``- [x]`` steps with SHA annotations, or
- every such step's commit adds at least one happy, edge, and sad test.

Missing PLAN.md or schema-invalid PLAN.md is a hard failure.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Reuse the audit gate's vocabulary verbatim so a test name classified
# happy/edge/sad at delta time keeps the same classification at audit
# time. Do not copy these keyword lists — import the live dict.
from peers.templates.modes.audit.checks.coverage_3class import KIND_RE
from peers_ctl.plan_parser import PlanValidationError, parse_plan

# Matches a `+def test_*` line in a unified diff (added test function).
# The leading `+` is required and we exclude `+++` (file header markers).
_ADDED_TEST_DEF_RE = re.compile(r"^\+def\s+(test_\w+)\s*\(")


def _added_test_names(project_root: Path, sha: str) -> list[str]:
    """Return test function names added by commit ``sha`` under tests/.

    Uses ``git show --diff-filter=AM`` so we see both new files (A)
    and modified files (M), restricted to ``tests/`` via a pathspec,
    then scans the diff for ``+def test_*`` lines.
    """
    proc = subprocess.run(
        [
            "git", "-C", str(project_root),
            "show", "--format=", "--diff-filter=AM", sha,
            "--", "tests/",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    names: list[str] = []
    for line in proc.stdout.splitlines():
        # Skip file header lines like `+++ b/tests/...`
        if line.startswith("+++"):
            continue
        m = _ADDED_TEST_DEF_RE.match(line)
        if m:
            names.append(m.group(1))
    return names


def _classify(name: str) -> set[str]:
    """Return the set of classes (subset of happy/edge/sad) ``name`` matches."""
    return {kind for kind, rx in KIND_RE.items() if rx.search(name)}


def main(project_dir: str = ".") -> int:
    """Per-step coverage gate: each [x] step's commit must add tests covering
    happy + edge + sad scenarios (mirrors audit-mode 3-class taxonomy).
    """
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("coverage-3class-delta FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"coverage-3class-delta FAIL: PLAN.md invalid: {e}")
        return 1

    checked = [s for s in plan.steps if s.state == "done"]
    if not checked:
        print("coverage-3class-delta: clean (no checked steps to verify)")
        return 0

    violations: list[str] = []
    deferred: list[str] = []
    verified = 0

    for step in checked:
        if not step.commit_sha:
            deferred.append(step.id)
            continue
        names = _added_test_names(project_root, step.commit_sha)
        if not names:
            violations.append(
                f"  {step.id}: no new test functions in commit "
                f"{step.commit_sha} (expected happy+edge+sad coverage)"
            )
            continue
        classes_present: set[str] = set()
        for name in names:
            classes_present |= _classify(name)
        # Only the canonical 3 classes count.
        present = classes_present & {"happy", "edge", "sad"}
        missing = {"happy", "edge", "sad"} - present
        if missing:
            violations.append(
                f"  {step.id}: missing classes: "
                f"{', '.join(sorted(missing))} "
                f"(commit {step.commit_sha} added {len(names)} test(s): "
                f"{', '.join(names)})"
            )
            continue
        verified += 1

    # Always surface deferred steps so the operator sees them.
    for sid in deferred:
        print(f"coverage-3class-delta: {sid} not verifiable (no commit sha)")

    if violations:
        print(
            f"coverage-3class-delta FAIL: "
            f"{len(violations)}/{len(checked) - len(deferred)} "
            f"verifiable step(s) lack 3-class coverage:"
        )
        for v in violations:
            print(v)
        return 1

    print(
        f"coverage-3class-delta: clean "
        f"({verified} step(s) cover happy+edge+sad, "
        f"{len(deferred)} skipped/deferred)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
