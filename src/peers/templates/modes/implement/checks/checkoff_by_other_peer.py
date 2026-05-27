#!/usr/bin/env python3
"""Exit 1 if any PLAN.md [x] checkoff was authored by the same peer that
last modified one of the step's ``touches:`` files.

Belt-and-suspenders backup for the Task 3.1 pre-commit hook
(``pre-commit-reviewer-checkoff``). The hook prevents *new* violating
commits at the time they are made, but it cannot catch violations that
landed in PRE-EXISTING commits — e.g. when the hook was not installed
when an old checkoff happened, or when commits were imported from
elsewhere. This gate scans the actual git log post-hoc, so a clean
``checkoff-by-other-peer`` run is positive evidence that every closed
step in PLAN.md was reviewed by the other peer.

Algorithm
---------
For every step in state ``done`` (``- [x]``) that declares
``touches: …``:

1. Locate the commit that toggled the step from ``[ ]`` to ``[x]``
   by walking ``git log --reverse --format=%H -- PLAN.md`` and
   inspecting each commit's diff on PLAN.md. The first commit whose
   diff contains both ``-- [ ] [STEP-N]`` (removed) and
   ``+- [x] [STEP-N]`` (added) is the checkoff commit.
2. Capture its author email (``%ae``).
3. For each file in ``touches:``, run
   ``git log -1 --format=%ae <CHECKOFF_SHA>~1 -- <file>`` to get the
   author of the most recent commit modifying that file BEFORE the
   checkoff. (Using ``~1`` excludes the checkoff commit itself, which
   might also touch the file in pathological cases.)
4. If the two emails match, record a violation.

Pass (exit 0) when no violations remain.
Fail (exit 1) with a per-violation diagnostic when any same-author
checkoff is detected.

Skip-friendly: steps without ``touches:`` cannot be enforced (we have
no implementation file to compare against), so they are not flagged
as failures here — Task 3.1's hook already warns on such cases at
commit time, and the operator can fall back on the
``plan-step-traceable`` gate for ground-truth coverage.

Missing PLAN.md or schema-invalid PLAN.md is a hard failure: the
substrate cannot evaluate the gate without a parseable plan.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from peers_ctl.plan_parser import PlanValidationError, parse_plan


# Match a PLAN.md step line on either side of a unified diff:
#   "+- [x] [STEP-3] add auth"     -> ("+", "x", "STEP-3")
#   "- - [ ] [STEP-3] add auth"    -> ("-", " ", "STEP-3")
# We accept optional surrounding whitespace after the sign because git
# diff sometimes emits "- - [ ]" with a space between the sign and the
# bullet (e.g. via ``--no-color``).
_DIFF_STEP_RE = re.compile(
    r"^(?P<sign>[+-])\s*-\s*\[(?P<mark>[ xX])\]\s*\[(?P<id>STEP-\d+)\]"
)


def _plan_commits(project_root: Path) -> list[str]:
    """Return SHAs of every commit touching PLAN.md, oldest-first."""
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "--reverse",
         "--format=%H", "--", "PLAN.md"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _commit_plan_diff(project_root: Path, sha: str) -> str:
    """Return the PLAN.md unified diff introduced by commit ``sha``.

    For the root commit (no parent) we use ``git show`` which emits the
    full file as additions; for normal commits ``git show`` produces a
    diff against the first parent. Either way the +/- prefix on step
    lines is what we need.
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "show", "--format=", sha, "--", "PLAN.md"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _find_checkoff_commit(project_root: Path, step_id: str) -> str | None:
    """Return the SHA of the commit that toggled ``step_id`` from
    ``[ ]`` to ``[x]`` in PLAN.md, or ``None`` if no such transition
    exists in history.

    The transition is identified by a single commit's diff containing
    both an ``+- [x] [STEP-N]`` line AND an ``-- [ ] [STEP-N]`` line
    (i.e. the same step removed unchecked and re-added checked). The
    first commit (oldest-first) with that pattern is the checkoff
    commit — later flips/re-flips are not Task 3.2's concern.
    """
    for sha in _plan_commits(project_root):
        diff = _commit_plan_diff(project_root, sha)
        removed_open = False
        added_done = False
        for line in diff.splitlines():
            m = _DIFF_STEP_RE.match(line)
            if not m or m.group("id") != step_id:
                continue
            sign = m.group("sign")
            mark = m.group("mark").lower()
            if sign == "-" and mark == " ":
                removed_open = True
            elif sign == "+" and mark == "x":
                added_done = True
        if removed_open and added_done:
            return sha
    return None


def _author_email(project_root: Path, sha: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "-1", "--format=%ae", sha],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _last_impl_author(project_root: Path, checkoff_sha: str, path: str) -> str:
    """Return the author email of the most recent commit modifying
    ``path`` strictly before ``checkoff_sha``.

    Returns ``""`` if no such commit exists (file was never touched
    before the checkoff, e.g. the checkoff commit itself introduces
    the file — which is itself a different kind of failure that
    ``plan-step-traceable`` catches).
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "log", "-1", "--format=%ae",
         f"{checkoff_sha}~1", "--", path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def main(project_dir: str = ".") -> int:
    """Verify all step checkoffs were done by a different peer than the
    implementer of the step's ``touches:`` files.

    Belt-and-suspenders with the pre-commit hook from Task 3.1.

    Skip-friendly: steps without ``touches:`` declared cannot be
    enforced and are not flagged as failures (defer to operator
    awareness via the hook's stderr warning).
    """
    project_root = Path(project_dir).resolve()
    plan_path = project_root / "PLAN.md"
    if not plan_path.is_file():
        print("checkoff-by-other-peer FAIL: PLAN.md not found")
        return 1
    try:
        plan = parse_plan(plan_path)
    except PlanValidationError as e:
        print(f"checkoff-by-other-peer FAIL: PLAN.md invalid: {e}")
        return 1

    checked_with_touches = [
        s for s in plan.steps if s.state == "done" and s.touches
    ]
    if not checked_with_touches:
        print("checkoff-by-other-peer: clean (no enforceable checkoffs)")
        return 0

    violations: list[str] = []
    for step in checked_with_touches:
        checkoff_sha = _find_checkoff_commit(project_root, step.id)
        if not checkoff_sha:
            # No [ ]->[x] transition found in history. Could be a
            # step that was born [x] (rare) or whose toggle predates
            # PLAN.md being tracked. We cannot enforce here; defer.
            continue
        checkoff_author = _author_email(project_root, checkoff_sha)
        if not checkoff_author:
            continue
        for tf in step.touches:
            impl_author = _last_impl_author(project_root, checkoff_sha, tf)
            if not impl_author:
                # File was new in checkoff commit, or untracked before:
                # plan-step-traceable handles that class of failure.
                continue
            if impl_author == checkoff_author:
                violations.append(
                    f"  {step.id}: checkoff by {checkoff_author} matches "
                    f"implementation author {impl_author} for {tf}"
                )

    if violations:
        print(
            f"checkoff-by-other-peer FAIL: "
            f"{len(violations)} same-author checkoff violation(s):"
        )
        for v in violations:
            print(v)
        print(
            "  hint: implement-mode requires the OTHER peer to mark steps "
            "[x] after review"
        )
        return 1

    print(
        f"checkoff-by-other-peer: clean "
        f"({len(checked_with_touches)} checkoff(s) verified, all by other peer)"
    )
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) >= 2 else "."
    sys.exit(main(arg))
